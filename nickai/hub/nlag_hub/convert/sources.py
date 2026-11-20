"""Where the weights of a Qwen3.5 model come from. Every source offers the same small interface, in terms of the original HuggingFace tensor
names and values (so one converter handles all of them):

    src.cfg                    the model's text config (HuggingFace names: hidden_size, layer_types, ...)
    src.get(name, r0, r1)      float32 tensor (or rows r0:r1 of a 2-D tensor), HuggingFace layout and conventions
    src.shape(name)            its shape

HFSource    reads a HuggingFace snapshot (config.json + *.safetensors): the original bf16 weights, best quality.
GGUFSource  reads an Ollama / llama.cpp GGUF (arch "qwen35"): dequantizes Q4_K/Q6_K/Q8_0/... and undoes llama.cpp's conversion (RMSNorm weights
            stored as 1+w, A stored as -exp(A_log), and the value heads of the linear-attention layers re-ordered from "grouped" to "tiled").
            Verified tensor by tensor against the HuggingFace weights of Qwen3.5-4B (relative error = the Q4_K rounding, 0 for the F32 tensors).
Input files are untrusted data: only headers are parsed and every offset is range-checked."""
import json
import mmap
import pathlib
import struct

import numpy as np

PREFIX = 'model.language_model.'


class Safetensors:
    """Minimal reader: 8-byte LE header length, JSON header, raw little-endian tensor bytes."""

    def __init__(self, path):
        self.f = open(path, 'rb')
        self.mm = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        size = len(self.mm)
        (n,) = struct.unpack('<Q', self.mm[:8])
        if not 0 < n < (64 << 20) or 8 + n > size:
            raise ValueError('bad safetensors header length')
        self.h = json.loads(bytes(self.mm[8:8 + n]))
        self.base = 8 + n
        self.size = size
        self.h.pop('__metadata__', None)
        for k, t in self.h.items():
            a, b = t['data_offsets']
            if not 0 <= a <= b <= size - self.base:
                raise ValueError(f'tensor {k} outside the file')

    def shape(self, name):
        return list(self.h[name]['shape'])

    def get(self, name, r0=None, r1=None):
        t = self.h[name]
        a, b = t['data_offsets']
        dt = t['dtype']
        shape = list(t['shape'])
        width = {'BF16': 2, 'F16': 2, 'F32': 4}.get(dt)
        if width is None:
            raise ValueError(f'unsupported dtype {dt} for {name}')
        if r0 is not None:
            if not (len(shape) == 2 and 0 <= r0 <= r1 <= shape[0]):
                raise ValueError('bad row range')
            rb = shape[1] * width
            a, b = a + r0 * rb, a + r1 * rb
            shape[0] = r1 - r0
        raw = np.frombuffer(self.mm, dtype=np.uint8, count=b - a, offset=self.base + a)
        if dt == 'BF16':
            v = (raw.view(np.uint16).astype(np.uint32) << 16).view(np.float32)
        elif dt == 'F16':
            v = raw.view(np.float16).astype(np.float32)
        else:
            v = raw.view(np.float32)
        return v.reshape(shape)


class HFSource:
    kind = 'hf'

    def __init__(self, directory):
        self.dir = pathlib.Path(directory)
        self.cfg = json.loads((self.dir / 'config.json').read_text())['text_config']
        self.wmap = json.loads((self.dir / 'model.safetensors.index.json').read_text())['weight_map']
        self.files = {}

    def _file(self, name):
        f = self.wmap[name]
        if f not in self.files:
            self.files[f] = Safetensors(self.dir / f)
        return self.files[f]

    def shape(self, name):
        return self._file(name).shape(name)

    def get(self, name, r0=None, r1=None):
        return self._file(name).get(name, r0, r1)

    def label(self):
        return self.dir.name


# --------------------------------------------------------------------------------------------------------------------------------- GGUF
class GGUFSource:
    kind = 'ollama'

    def __init__(self, path):
        import gguf   # pip install gguf (the container image has it)
        self.reader = gguf.GGUFReader(str(path))
        self.gguf = gguf
        self.path = str(path)
        self.tensors = {t.name: t for t in self.reader.tensors}
        meta = {}
        for k, f in self.reader.fields.items():
            if k.startswith(('general.architecture', 'general.size_label', 'general.name')) or k.startswith('qwen35.'):
                try:
                    meta[k] = f.contents()
                except Exception:  # noqa: BLE001 - metadata we do not need may be of a type the reader cannot decode
                    pass
        self.meta = meta
        arch = str(meta.get('general.architecture', ''))
        if arch != 'qwen35':
            raise ValueError(f'unsupported architecture {arch!r}: the Garlic engine runs Qwen3.5 (GGUF architecture "qwen35") only')
        g = lambda k: meta['qwen35.' + k]  # noqa: E731
        emb = self.tensors['token_embd.weight']
        head_dim = int(g('attention.key_length'))
        rec = [bool(x) for x in g('attention.recurrent_layers')]
        self.cfg = {
            'hidden_size': int(g('embedding_length')), 'intermediate_size': int(g('feed_forward_length')), 'num_hidden_layers': int(g('block_count')),
            'num_attention_heads': int(g('attention.head_count')), 'num_key_value_heads': int(g('attention.head_count_kv')), 'head_dim': head_dim,
            'vocab_size': int(emb.shape[1]), 'rms_norm_eps': float(g('attention.layer_norm_rms_epsilon')),
            'rope_parameters': {'rope_theta': float(g('rope.freq_base')), 'partial_rotary_factor': int(g('rope.dimension_count')) / head_dim},
            'linear_conv_kernel_dim': int(g('ssm.conv_kernel')), 'linear_key_head_dim': int(g('ssm.state_size')), 'linear_num_key_heads': int(g('ssm.group_count')),
            'linear_num_value_heads': int(g('ssm.time_step_rank')), 'linear_value_head_dim': int(g('ssm.inner_size')) // int(g('ssm.time_step_rank')),
            'layer_types': ['linear_attention' if r else 'full_attention' for r in rec]}
        if len(rec) != self.cfg['num_hidden_layers']:
            raise ValueError('layer type list does not match the block count')
        self.nk, self.nv = self.cfg['linear_num_key_heads'], self.cfg['linear_num_value_heads']
        self.hv = self.cfg['linear_value_head_dim']
        if self.nv % self.nk:
            raise ValueError('value heads are not a multiple of key heads')
        self.per = self.nv // self.nk

    def label(self):
        return str(self.meta.get('general.size_label') or self.meta.get('general.name') or 'gguf')

    # -- raw GGUF tensors -------------------------------------------------------------------------------------------------------
    def _raw(self, gname, r0=None, r1=None):
        t = self.tensors[gname]
        shape = [int(x) for x in t.shape[::-1]]   # GGUF lists the fastest dimension first
        data = t.data
        if r0 is not None:
            if len(shape) != 2 or not 0 <= r0 <= r1 <= shape[0]:
                raise ValueError('bad row range')
            data, shape = data[r0:r1], [r1 - r0, shape[1]]
        name = t.tensor_type.name
        if name in ('F32', 'F16', 'BF16'):
            if name == 'BF16':
                v = (np.asarray(data).view(np.uint16).astype(np.uint32) << 16).view(np.float32)
            else:
                v = np.asarray(data, dtype=np.float32)
        else:
            v = np.asarray(self.gguf.quants.dequantize(data, t.tensor_type), dtype=np.float32)
        return v.reshape(shape)

    def _untile(self, x, axis, block):
        """Undo llama.cpp's "tiled" order of the value heads along `axis` (blocks of `block` entries): index r*nk+k -> k*per+r."""
        if self.per == 1:
            return x
        x = np.moveaxis(x, axis, 0)
        s = x.shape
        rest = s[1:]
        y = x.reshape(self.per, self.nk, block, *rest).transpose(1, 0, 2, *range(3, 3 + len(rest))).reshape(s)
        return np.moveaxis(y, 0, axis)

    def shape(self, name):
        return list(self._map(name)[1]())

    # -- the HuggingFace view ---------------------------------------------------------------------------------------------------
    def _map(self, name):
        """-> (get(r0, r1), shape()) for a HuggingFace tensor name."""
        c = self.cfg
        if name == PREFIX + 'embed_tokens.weight':
            return (lambda r0=None, r1=None: self._raw('token_embd.weight', r0, r1)), (lambda: (c['vocab_size'], c['hidden_size']))
        if name == PREFIX + 'norm.weight':
            return (lambda r0=None, r1=None: self._raw('output_norm.weight') - 1.0), (lambda: (c['hidden_size'],))
        if not name.startswith(PREFIX + 'layers.'):
            raise KeyError(name)
        rest = name[len(PREFIX + 'layers.'):]
        i, _, tail = rest.partition('.')
        b = f'blk.{int(i)}.'
        direct = {'input_layernorm.weight': ('attn_norm.weight', -1.0), 'post_attention_layernorm.weight': ('post_attention_norm.weight', -1.0),
                  'self_attn.q_norm.weight': ('attn_q_norm.weight', -1.0), 'self_attn.k_norm.weight': ('attn_k_norm.weight', -1.0),
                  'self_attn.q_proj.weight': ('attn_q.weight', 0), 'self_attn.k_proj.weight': ('attn_k.weight', 0), 'self_attn.v_proj.weight': ('attn_v.weight', 0),
                  'self_attn.o_proj.weight': ('attn_output.weight', 0), 'mlp.gate_proj.weight': ('ffn_gate.weight', 0), 'mlp.up_proj.weight': ('ffn_up.weight', 0),
                  'mlp.down_proj.weight': ('ffn_down.weight', 0), 'linear_attn.norm.weight': ('ssm_norm.weight', 0)}
        if tail in direct:
            g, add = direct[tail]
            return (lambda r0=None, r1=None: self._raw(b + g, r0, r1) + add if add else self._raw(b + g, r0, r1)), (lambda: [int(x) for x in self.tensors[b + g].shape[::-1]])
        nk2 = self.nk * c['linear_key_head_dim']   # rows of q (= rows of k) in the fused qkv projection
        hv = self.hv
        if tail == 'linear_attn.in_proj_qkv.weight':
            def f(r0=None, r1=None):
                w = self._raw(b + 'attn_qkv.weight')
                return np.concatenate([w[:2 * nk2], self._untile(w[2 * nk2:], 0, hv)])
            return f, (lambda: [int(x) for x in self.tensors[b + 'attn_qkv.weight'].shape[::-1]])
        if tail == 'linear_attn.in_proj_z.weight':
            return (lambda r0=None, r1=None: self._untile(self._raw(b + 'attn_gate.weight'), 0, hv)), (lambda: [int(x) for x in self.tensors[b + 'attn_gate.weight'].shape[::-1]])
        if tail == 'linear_attn.in_proj_b.weight':
            return (lambda r0=None, r1=None: self._untile(self._raw(b + 'ssm_beta.weight'), 0, 1)), (lambda: [int(x) for x in self.tensors[b + 'ssm_beta.weight'].shape[::-1]])
        if tail == 'linear_attn.in_proj_a.weight':
            return (lambda r0=None, r1=None: self._untile(self._raw(b + 'ssm_alpha.weight'), 0, 1)), (lambda: [int(x) for x in self.tensors[b + 'ssm_alpha.weight'].shape[::-1]])
        if tail == 'linear_attn.out_proj.weight':
            return (lambda r0=None, r1=None: self._untile(self._raw(b + 'ssm_out.weight'), 1, hv)), (lambda: [int(x) for x in self.tensors[b + 'ssm_out.weight'].shape[::-1]])
        if tail == 'linear_attn.conv1d.weight':
            def fc(r0=None, r1=None):
                w = self._raw(b + 'ssm_conv1d.weight')
                return np.concatenate([w[:2 * nk2], self._untile(w[2 * nk2:], 0, hv)]).reshape(w.shape[0], 1, w.shape[1])
            return fc, (lambda: (int(self.tensors[b + 'ssm_conv1d.weight'].shape[1]), 1, int(self.tensors[b + 'ssm_conv1d.weight'].shape[0])))
        if tail == 'linear_attn.A_log':
            return (lambda r0=None, r1=None: np.log(-self._untile(self._raw(b + 'ssm_a'), 0, 1))), (lambda: (self.nv,))
        if tail == 'linear_attn.dt_bias':
            return (lambda r0=None, r1=None: self._untile(self._raw(b + 'ssm_dt.bias'), 0, 1)), (lambda: (self.nv,))
        raise KeyError(name)

    def get(self, name, r0=None, r1=None):
        f, _ = self._map(name)
        if r0 is not None:
            if name == PREFIX + 'embed_tokens.weight':
                return f(r0, r1)
            return f()[r0:r1]
        return f()


def open_source(path):
    """A directory with config.json -> HFSource; a .gguf file (or Ollama blob) -> GGUFSource."""
    p = pathlib.Path(path)
    if p.is_dir():
        return HFSource(p)
    return GGUFSource(p)
