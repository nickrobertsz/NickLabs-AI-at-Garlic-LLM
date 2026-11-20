"""Where models come from: the user's own Ollama store (no download), the Ollama registry, HuggingFace. Blocking code (run in job threads).

Everything fetched from the network or read from a store is untrusted data: sizes and digests are checked, names are validated, nothing is executed."""
import hashlib
import json
import os
import pathlib
import re
import urllib.error
import urllib.request

from . import core

UA = 'nickLabs-ai-on-garlic/1.0'
OLLAMA_REGISTRY = 'https://registry.ollama.ai'
HF = 'https://huggingface.co'
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$')
REPO_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,100}$')


class SourceError(Exception):
    pass


def ollama_dirs():
    """Candidate Ollama model stores (the folder that holds manifests/ and blobs/)."""
    cands = []
    for e in (os.environ.get('NLAG_OLLAMA_MODELS'), os.environ.get('OLLAMA_MODELS')):
        if e:
            cands.append(pathlib.Path(e))
    cands += [pathlib.Path('/ollama-models'), pathlib.Path.home() / '.ollama' / 'models', pathlib.Path('/usr/share/ollama/.ollama/models'), pathlib.Path('/var/lib/ollama/.ollama/models')]
    out, seen = [], set()
    for c in cands:
        try:
            if (c / 'manifests').is_dir() and (c / 'blobs').is_dir() and str(c.resolve()) not in seen:
                seen.add(str(c.resolve()))
                out.append(c)
        except OSError:
            continue
    return out


def _gguf_info(path):
    """(architecture, size_label, file_type) from a GGUF header without reading the weights; None if it is not a readable GGUF."""
    try:
        import gguf
        r = gguf.GGUFReader(str(path))
        g = lambda k: (r.fields[k].contents() if k in r.fields else None)  # noqa: E731
        return str(g('general.architecture') or ''), str(g('general.size_label') or ''), g('general.file_type')
    except Exception:  # noqa: BLE001 - a store may contain anything
        return None


def list_ollama_local():
    """Models in the user's Ollama store: [{"ref": "qwen3.5:4b", "blob": path, "bytes": n, "arch": "qwen35", "supported": bool, "reason": str}]."""
    out = []
    for root in ollama_dirs():
        base = root / 'manifests'
        for mf in base.rglob('*'):
            if not mf.is_file():
                continue
            rel = mf.relative_to(base).parts      # registry.ollama.ai / library / qwen3.5 / 4b
            if len(rel) < 4:
                continue
            host, tag, name = rel[0], rel[-1], '/'.join(rel[1:-1])
            ref = (name[len('library/'):] if name.startswith('library/') else name) + ':' + tag
            if host != 'registry.ollama.ai':
                ref = host + '/' + ref
            try:
                m = json.loads(mf.read_text())
                layer = next(l for l in m['layers'] if l.get('mediaType', '').endswith('image.model'))
                digest = layer['digest']
            except (OSError, ValueError, StopIteration, KeyError, TypeError):
                continue
            blob = root / 'blobs' / digest.replace(':', '-')
            if not blob.is_file():
                continue
            info = _gguf_info(blob)
            arch = info[0] if info else ''
            supported = arch == 'qwen35'
            reason = '' if supported else (f'architecture {arch!r} is not supported: the Garlic engine runs Qwen3.5 models' if arch else 'not a readable GGUF file')
            est = 0
            if supported:
                try:   # does it fit in one console's GPU memory?
                    from .convert import qwen35, sources
                    gcfg = sources.GGUFSource(blob).cfg
                    ok, est = qwen35.fits(gcfg)
                    if est > qwen35.GPU_WEIGHT_BUDGET:
                        supported, reason = False, f'needs about {est / 1e9:.1f} GB of weights; one console holds about {qwen35.GPU_WEIGHT_BUDGET / 1e9:.1f} GB (splitting a model over several consoles is not implemented yet)'
                    elif qwen35.shape_problem(gcfg):
                        supported, reason = False, qwen35.shape_problem(gcfg)
                except Exception as exc:  # noqa: BLE001 - a store may contain anything
                    supported, reason = False, f'cannot read the model: {type(exc).__name__}: {exc}'
            out.append({'ref': ref, 'blob': str(blob), 'bytes': blob.stat().st_size, 'arch': arch, 'size_label': info[1] if info else '', 'supported': supported, 'reason': reason, 'digest': digest, 'est_bytes': est})
    return sorted(out, key=lambda x: x['ref'])


def find_ollama_local(ref):
    for m in list_ollama_local():
        if m['ref'] == ref or m['ref'] == ref + ':latest':
            return m
    raise SourceError(f'{ref!r} is not in a local Ollama store (found: {[m["ref"] for m in list_ollama_local()]})')


# ---------------------------------------------------------------------------------------------------------------------------- downloads
def _open(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def download(url, dest, expect_size=None, sha256=None, progress=None, cancelled=lambda: False, headers=None):
    """Resumable download to `dest` (a partial `dest.part` is continued). Verifies size and SHA-256 when given."""
    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and (expect_size is None or dest.stat().st_size == expect_size):
        if sha256 is None or sha256_file(dest) == sha256:
            if progress:
                progress(dest.stat().st_size, dest.stat().st_size)
            return dest
    part = dest.with_suffix(dest.suffix + '.part')
    have = part.stat().st_size if part.exists() else 0
    h = {**(headers or {})}
    if have:
        h['Range'] = f'bytes={have}-'
    try:
        r = _open(url, h, 120)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:       # already complete
            r = None
        else:
            raise SourceError(f'download failed: HTTP {exc.code} for {url}')
    except (urllib.error.URLError, OSError) as exc:
        raise SourceError(f'download failed: {exc}')
    if r is not None:
        total = (int(r.headers.get('Content-Length') or 0) + (have if r.status == 206 else 0)) or expect_size
        if r.status != 206:
            have = 0
        with open(part, 'ab' if have else 'wb') as f, r:
            got = have
            while True:
                if cancelled():
                    raise InterruptedError('cancelled')
                try:
                    b = r.read(1 << 20)
                except OSError as exc:
                    raise SourceError(f'download interrupted: {exc} (run it again to resume)')
                if not b:
                    break
                f.write(b)
                got += len(b)
                if progress:
                    progress(got, total or got)
    size = part.stat().st_size
    if expect_size is not None and size != expect_size:
        raise SourceError(f'download incomplete: {size} of {expect_size} bytes (run it again to resume)')
    if sha256 is not None and sha256_file(part) != sha256:
        part.unlink()
        raise SourceError('download corrupt: SHA-256 differs from the registry digest; it was deleted, run it again')
    os.replace(part, dest)
    return dest


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def parse_ollama_ref(ref):
    """'qwen3.5:4b' -> ('library/qwen3.5', '4b'); 'user/model:tag' -> ('user/model', 'tag')."""
    ref = ref.strip()
    name, _, tag = ref.partition(':')
    tag = tag or 'latest'
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?', name) or not re.fullmatch(r'[A-Za-z0-9._-]+', tag):
        raise SourceError(f'bad model reference {ref!r}')
    return (name if '/' in name else 'library/' + name), tag


def pull_ollama(ref, cache_dir, progress=None, cancelled=lambda: False, log=lambda m: None):
    """Download a model's GGUF from the Ollama registry into the cache (no Ollama needed). Returns the blob path."""
    repo, tag = parse_ollama_ref(ref)
    url = f'{OLLAMA_REGISTRY}/v2/{repo}/manifests/{tag}'
    try:
        with _open(url, {'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}) as r:
            m = json.loads(r.read(1 << 20))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SourceError(f'cannot read the Ollama manifest for {ref}: {exc}')
    layer = next((l for l in m.get('layers', []) if str(l.get('mediaType', '')).endswith('image.model')), None)
    if not layer or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(layer.get('digest', ''))):
        raise SourceError(f'{ref}: the manifest has no model layer')
    digest, size = layer['digest'], int(layer['size'])
    log(f'{ref}: model file {size / 1e9:.2f} GB')
    dest = pathlib.Path(cache_dir) / digest.replace(':', '-')
    download(f'{OLLAMA_REGISTRY}/v2/{repo}/blobs/{digest}', dest, size, digest.split(':')[1], progress, cancelled)
    return dest


def pull_hf(repo, dest_dir, progress=None, cancelled=lambda: False, log=lambda m: None, revision='main'):
    """Download config.json + safetensors (+ index) of a HuggingFace repo into dest_dir. Returns the directory."""
    if not REPO_RE.match(repo):
        raise SourceError(f'bad HuggingFace repository {repo!r} (expected owner/name)')
    try:
        with _open(f'{HF}/api/models/{repo}?blobs=true') as r:
            info = json.loads(r.read(8 << 20))
    except urllib.error.HTTPError as exc:
        raise SourceError(f'HuggingFace: HTTP {exc.code} for {repo} ({"gated or private: this tool does not use tokens" if exc.code in (401, 403) else "not found"})')
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SourceError(f'HuggingFace: {exc}')
    files = {s['rfilename']: s for s in info.get('siblings', []) if isinstance(s, dict) and isinstance(s.get('rfilename'), str)}
    want = [f for f in files if f in ('config.json', 'model.safetensors.index.json', 'LICENSE') or re.fullmatch(r'model[\w.-]*\.safetensors', f)]
    if 'config.json' not in want or not any(f.endswith('.safetensors') for f in want):
        raise SourceError(f'{repo} has no safetensors weights')
    total = sum(int(files[f].get('size') or 0) for f in want)
    done = [0]
    d = pathlib.Path(dest_dir)
    for f in sorted(want):
        size = int(files[f].get('size') or 0) or None
        base = done[0]
        download(f'{HF}/{repo}/resolve/{revision}/{f}', d / f, size, None, (lambda g, t, b=base: progress(b + g, total or b + g)) if progress else None, cancelled)
        done[0] += size or 0
        log(f'{f} ok')
    if not (d / 'model.safetensors.index.json').exists():     # a single-file checkpoint: synthesize the index the converter expects
        st = next(p for p in d.iterdir() if p.suffix == '.safetensors')
        from .convert.sources import Safetensors
        names = Safetensors(st).h.keys()
        (d / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': {n: st.name for n in names}}))
    return d


def slug(text):
    s = re.sub(r'[^a-z0-9.]+', '-', text.lower()).strip('-')
    return s[:48] or 'model'
