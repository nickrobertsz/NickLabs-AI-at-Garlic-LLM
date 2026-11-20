"""Qwen3.5 -> Garlic "q4i" bundle, entirely on the CPU (numpy only, no torch, no GPU).

A faithful port of Garlic LLM's pc/modal_qwen35.py::convert_remote (same tensor order, same folding, same q4 quantizer) followed by pc/relayout_q4i.py,
reading any `sources.py` source, then the model index of pc/export_chat.py::export_model_index_qwen35 and the bundle manifest the console app validates.

    convert(src, out_dir, name, progress)   -> bundle directory with model.idx kernels.bin tokenizer.bin <weights> bundle.json (+ the trust record)

Sizes: the engine keeps ALL weights in the console's GPU memory (about 4.6 GB are available), so `fits()` refuses what cannot load."""
import hashlib
import json
import os
import pathlib
import shutil
import struct

import numpy as np

from .q4 import interleave_q4, quantize_q4
from .sources import PREFIX

CHUNK_ROWS = 16384
MAX_SEQ = 8192                       # what the shipped Qwen3.5 model.idx files declare (the engine clamps contexts to its own ceiling)
NAME_BY_HIDDEN = {1024: '0.8b', 2048: '2b', 2560: '4b', 4096: '9b', 5120: '27b'}
GPU_WEIGHT_BUDGET = 4_600_000_000    # bytes of weights a console can hold (GPU arena minus KV cache / scratch); measured: the 4B (2.37 GB) loads, the shipped 9B is 4.86 GB split Q4/Q3


def size_label(cfg, hint=''):
    """'4b' etc. from a name hint like 'qwen3.5:4b' / 'Qwen3.5-4B', else from the hidden size."""
    import re
    m = re.search(r'(\d+(?:\.\d+)?)\s*b\b', hint.lower().replace('-', ' ').replace(':', ' ').replace('_', ' '))
    if m:
        return m.group(1) + 'b'
    return NAME_BY_HIDDEN.get(cfg['hidden_size'], f"{cfg['hidden_size']}h")


def estimate_bytes(cfg):
    """Size of the q4i weights file for this config (4-bit weights + one fp16 scale per 32, f32 norms)."""
    h, i, L, v = cfg['hidden_size'], cfg['intermediate_size'], cfg['num_hidden_layers'], cfg['vocab_size']
    nh, nkv, hd = cfg['num_attention_heads'], cfg['num_key_value_heads'], cfg['head_dim']
    total = v * h * 9 // 16
    for kind in cfg['layer_types']:
        if kind == 'full_attention':
            total += (nh * hd * 2 + 2 * nkv * hd) * h * 9 // 16 + h * nh * hd * 9 // 16
        else:
            kd = cfg['linear_key_head_dim'] * cfg['linear_num_key_heads']
            vd = cfg['linear_value_head_dim'] * cfg['linear_num_value_heads']
            total += (2 * kd + vd + vd + 2 * cfg['linear_num_value_heads']) * h * 9 // 16 + h * vd * 9 // 16
        total += 3 * h * i * 9 // 16
    return total + L * 4 * 8192


def shape_problem(cfg):
    """'' if the console's q4i layout can hold this model, else why not: every quantized matrix needs a multiple of 64 rows (tiles) and of 32 columns."""
    h, i, v = cfg['hidden_size'], cfg['intermediate_size'], cfg['vocab_size']
    nh, nkv, hd = cfg['num_attention_heads'], cfg['num_key_value_heads'], cfg['head_dim']
    kd, vd = cfg['linear_key_head_dim'] * cfg['linear_num_key_heads'], cfg['linear_value_head_dim'] * cfg['linear_num_value_heads']
    rows = {'embedding': v, 'attention qkv': 2 * nh * hd + 2 * nkv * hd, 'attention out': h, 'linear-attention input': 2 * kd + 2 * vd + 2 * cfg['linear_num_value_heads'],
            'linear-attention out': h, 'mlp gate/up': 2 * i, 'mlp down': h}
    for what, r in rows.items():
        if r % 64:
            return f'the {what} matrix has {r} rows, not a multiple of 64: the console\'s weight layout works in tiles of 64 rows, so this model size cannot run on the engine as it is'
    return ''


def fits(cfg):
    n = estimate_bytes(cfg)
    return n <= GPU_WEIGHT_BUDGET and not shape_problem(cfg), n


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def convert(src, out_dir, name='qwen3.5-garlic', progress=print, cancelled=lambda: False):
    """Write OUT/<name>-q4.bin/.json and the console file OUT/<name>-q4i.bin/.json. Returns the meta dict of the q4i file."""
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = src.cfg
    ok, est = fits(cfg)
    if not ok:
        raise ValueError(f'this model needs about {est / 1e9:.1f} GB of weights; a console holds about {GPU_WEIGHT_BUDGET / 1e9:.1f} GB (the engine keeps all weights in GPU memory)')
    get = src.get
    o = open(out / f'{name}-q4.bin', 'wb')
    index, offset = {}, 0

    def put(key, data, **meta):
        nonlocal offset
        pad = (-offset) % 256
        o.write(bytes(pad))
        offset += pad
        o.write(data)
        index[key] = dict(offset=offset, size=len(data), **meta)
        offset += len(data)

    def put_f32(key, a):
        put(key, np.ascontiguousarray(a, dtype=np.float32).tobytes(), n=int(np.size(a)))

    def put_q4(key, mat, rows=None, cols=None):
        if callable(mat):
            chunk = mat
        else:
            m = np.ascontiguousarray(mat, dtype=np.float32)
            rows, cols = m.shape
            chunk = lambda a, b: m[a:b]  # noqa: E731
        if cols % 32:
            raise ValueError(f'{key}: {cols} columns is not a multiple of 32')
        packed, scales = [], []
        for r in range(0, rows, CHUNK_ROWS):
            p, d = quantize_q4(np.ascontiguousarray(chunk(r, min(r + CHUNK_ROWS, rows)), dtype=np.float32))
            packed.append(p.tobytes())
            scales.append(d.tobytes())
        pk = b''.join(packed)
        put(key, pk + b''.join(scales), rows=rows, cols=cols, scales=len(pk), fmt='q4')

    E = PREFIX + 'embed_tokens.weight'
    er, ec = src.shape(E)
    put_q4('embed', lambda a, b: get(E, a, b), rows=er, cols=ec)
    put_f32('final_norm', 1.0 + get(PREFIX + 'norm.weight'))
    nh, hd = cfg['num_attention_heads'], cfg['head_dim']
    n_layers = len(cfg['layer_types'])
    for i, kind in enumerate(cfg['layer_types']):
        if cancelled():
            raise InterruptedError('cancelled')
        p = f'{PREFIX}layers.{i}.'
        put_f32(f'{i}.attn_norm', 1.0 + get(p + 'input_layernorm.weight'))
        put_f32(f'{i}.mlp_norm', 1.0 + get(p + 'post_attention_layernorm.weight'))
        if kind == 'full_attention':
            a = p + 'self_attn.'
            put_f32(f'{i}.q_norm', 1.0 + get(a + 'q_norm.weight'))
            put_f32(f'{i}.k_norm', 1.0 + get(a + 'k_norm.weight'))
            qg = get(a + 'q_proj.weight').reshape(nh, 2, hd, -1)             # per head: [query | gate]
            q, gate = qg[:, 0].reshape(nh * hd, -1), qg[:, 1].reshape(nh * hd, -1)
            put_q4(f'{i}.wqkv', np.concatenate([q, get(a + 'k_proj.weight'), get(a + 'v_proj.weight'), gate]))
            put_q4(f'{i}.wo', get(a + 'o_proj.weight'))
        else:
            a = p + 'linear_attn.'
            put_q4(f'{i}.w_in', np.concatenate([get(a + 'in_proj_qkv.weight'), get(a + 'in_proj_z.weight'), get(a + 'in_proj_b.weight'), get(a + 'in_proj_a.weight')]))
            put_f32(f'{i}.conv', get(a + 'conv1d.weight').reshape(-1, cfg['linear_conv_kernel_dim']))
            put_f32(f'{i}.A', -np.exp(get(a + 'A_log')))
            put_f32(f'{i}.dt_bias', get(a + 'dt_bias'))
            put_f32(f'{i}.gnorm', get(a + 'norm.weight'))
            put_q4(f'{i}.wout', get(a + 'out_proj.weight'))
        put_q4(f'{i}.wgu', np.concatenate([get(p + 'mlp.gate_proj.weight'), get(p + 'mlp.up_proj.weight')]))
        put_q4(f'{i}.wdown', get(p + 'mlp.down_proj.weight'))
        progress(f'layer {i + 1}/{n_layers} ({kind}), {offset / 2 ** 20:.0f} MiB', (i + 1) / n_layers * 0.9)
    o.close()
    meta = dict(config=cfg, size=offset, tensors=index)
    (out / f'{name}-q4.json').write_text(json.dumps(meta, indent=1))

    # relayout q4 -> q4i (pc/relayout_q4i.py: same tensors/offsets/sizes, interleaved tiles of 64 rows)
    src_f = open(out / f'{name}-q4.bin', 'rb')
    dst = open(out / f'{name}-q4i.bin', 'wb')
    pos = 0
    for key, t in sorted(meta['tensors'].items(), key=lambda kv: kv[1]['offset']):
        if t['offset'] > pos:
            dst.write(bytes(t['offset'] - pos))
        src_f.seek(t['offset'])
        data = src_f.read(t['size'])
        if t.get('fmt') == 'q4':
            rows, cols = t['rows'], t['cols']
            if rows % 64:
                raise ValueError(f'{key}: {rows} rows is not a multiple of 64 (the q4i layout works in tiles of 64 rows): this model shape is not supported by the engine')
            packed = np.frombuffer(data[:t['scales']], np.uint8).reshape(rows, cols // 2)
            d16 = np.frombuffer(data[t['scales']:], np.float16).reshape(rows, cols // 32)
            nib, sc = interleave_q4(packed, d16)
            data = nib + sc
            t['fmt'] = 'q4i'
        dst.write(data)
        pos = t['offset'] + t['size']
    dst.close()
    src_f.close()
    (out / f'{name}-q4i.json').write_text(json.dumps(meta, indent=1))
    os.unlink(out / f'{name}-q4.bin')   # only the console layout is kept (the plain q4 file is the same size again)
    progress(f'wrote {name}-q4i.bin ({pos / 1e9:.2f} GB)', 0.95)
    return meta


# ------------------------------------------------------------------------------------------------------------------ model index + bundle
def model_idx(meta, name):
    """Port of export_model_index_qwen35 (same field order and packing)."""
    c, t = meta['config'], meta['tensors']
    order, mask = ['embed', 'final_norm'], 0
    for i, kind in enumerate(c['layer_types']):
        full = kind == 'full_attention'
        mask |= int(full) << i
        mixer = ('q_norm', 'k_norm', 'wqkv', 'wo') if full else ('w_in', 'conv', 'A', 'dt_bias', 'gnorm', 'wout')
        order += [f'{i}.{k}' for k in ('attn_norm', 'mlp_norm', *mixer, 'wgu', 'wdown')]
    flags = 0
    if 'lm_head' in t:
        order.append('lm_head')
        flags |= 1
    if t['embed'].get('fmt') == 'q4row':
        flags |= 2
    rp = c['rope_parameters']
    rope_dim = int(c['head_dim'] * rp.get('partial_rotary_factor', 1.0))
    path = f'/data/{name}.bin'.encode()
    head = struct.pack('<4s10I2f128sQI', b'PMQ5', c['num_hidden_layers'], c['hidden_size'], c['intermediate_size'], c['num_attention_heads'], c['num_key_value_heads'],
                       c['head_dim'], c['vocab_size'], MAX_SEQ, 248046, 248044, c['rms_norm_eps'], rp['rope_theta'], path, meta.get('gpu_size', meta['size']), len(order))
    head += struct.pack('<8I', c['linear_num_key_heads'], c['linear_num_value_heads'], c['linear_key_head_dim'], c['linear_value_head_dim'], rope_dim, mask, 248045, flags)
    recs = []
    for k in order:
        e = t[k]
        fmt = {'q4': 2, 'q8': 1, 'q4i': 3, 'q4row': 4, 'q3i': 5}.get(e.get('fmt'), 0)
        recs.append(struct.pack('<QQIIQI', e['offset'], e['size'], e.get('rows', 0), e.get('cols', 0), e.get('scales', 0), fmt))
    return head + b''.join(recs)


def make_bundle(build_dir, out_dir, name, arch, assets_dir, display_name=None, quantization='q4i'):
    """Assemble the console bundle (hard-linking the big file when possible). Returns {"dir", "trust", "weights_bytes", "name"}.
    assets_dir holds Garlic's kernels.bin and tokenizer.bin (chat/assets in the Garlic repository)."""
    build, out, assets = pathlib.Path(build_dir), pathlib.Path(out_dir), pathlib.Path(assets_dir)
    wname = f'{name}-q4i.bin'
    meta = json.loads((build / f'{name}-q4i.json').read_text())
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / 'model.idx').write_bytes(model_idx(meta, f'{name}-q4i'))
    for f in ('kernels.bin', 'tokenizer.bin'):
        shutil.copyfile(assets / f, out / f)
    try:
        os.link(build / wname, out / wname)
    except OSError:
        shutil.copyfile(build / wname, out / wname)
    files = {p.name: {'bytes': p.stat().st_size, 'sha256': sha(p)} for p in sorted(out.iterdir())}
    manifest = {'version': 1, 'architecture': arch, 'name': display_name or f'{arch}-garlic', 'context_length': MAX_SEQ, 'weights': wname, 'files': files,
                'template': 'garlic-qwen35-no-thinking-v1', 'quantization': quantization, 'mtp': False}
    (out / 'bundle.json').write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    trust = {'model.idx': files['model.idx']['sha256'], 'kernels.bin': files['kernels.bin']['sha256'], 'tokenizer.bin': files['tokenizer.bin']['sha256'], 'weights': files[wname]['sha256']}
    return {'dir': str(out), 'trust': trust, 'weights_bytes': files[wname]['bytes'], 'name': manifest['name'], 'files': files}
