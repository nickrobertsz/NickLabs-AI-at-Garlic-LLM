"""Test fixture helpers. make_bundle_variant: Fat PS4 worker draft (Path.truncate does not exist, double write, wrong total: fixed by Claude).
free_port / wait_until: Slim PS4 worker draft (predicate must run at least once: fixed by Claude)."""
import hashlib, json, os, shutil, socket, time

SMALL = ("bundle.json", "kernels.bin", "model.idx", "mtp.idx", "tokenizer.bin")
BIG = ("qwen3.5-9b-mtp.bin", "qwen3.5-9b-q4s.bin")

def make_bundle_variant(src, dst, name):
    os.makedirs(dst, exist_ok=True)
    for f in SMALL + BIG:
        if not os.path.isfile(os.path.join(src, f)):
            raise FileNotFoundError(f"source file missing: {f}")
    for f in SMALL:
        if f == "bundle.json":
            with open(os.path.join(src, f)) as fh:
                obj = json.load(fh)
            obj["name"] = name
            with open(os.path.join(dst, f), "w") as fh:
                fh.write(json.dumps(obj, indent=2, sort_keys=True))
        else:
            shutil.copyfile(os.path.join(src, f), os.path.join(dst, f))
    for f in BIG:   # sparse placeholders of the exact size: the mock engine never reads weights
        with open(os.path.join(dst, f), "wb") as fh:
            fh.truncate(os.path.getsize(os.path.join(src, f)))
    with open(os.path.join(dst, "bundle.json"), "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    return {"dir": dst, "bundle_sha256": sha, "total_bytes": sum(os.path.getsize(os.path.join(dst, f)) for f in SMALL + BIG)}

def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

def wait_until(predicate, timeout=30.0, interval=0.1, what="condition"):
    deadline = time.monotonic() + timeout; last = None
    while True:
        try:
            r = predicate()
            if r:
                return r
        except Exception as e:
            last = e
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}" + (f": last error {last!r}" if last is not None else ""))


def make_bundle_4b(dst, root, name='qwen3.5-4b-garlic', extra_manifest=None, extra_files=()):
    """A qwen3.5-4b bundle from the REAL shipped small files (assets_4b/model.idx, assets/kernels.bin, assets/tokenizer.bin) + a sparse placeholder for the
    weights of the exact size the index needs. Returns (bundle dir, trust record). The mock engine never reads weights; the host never hashes them."""
    import struct
    chat = os.path.join(root, 'sources/garlic-llm/chat'); os.makedirs(dst, exist_ok=True)
    shutil.copyfile(os.path.join(chat, 'assets_4b/model.idx'), os.path.join(dst, 'model.idx'))
    for f in ('kernels.bin', 'tokenizer.bin'): shutil.copyfile(os.path.join(chat, 'assets', f), os.path.join(dst, f))
    idx = open(os.path.join(dst, 'model.idx'), 'rb').read(); n = struct.unpack('<I', idx[188:192])[0]; size = 0
    for i in range(n):
        off, sz = struct.unpack('<QQ', idx[224 + 36 * i:224 + 36 * i + 16]); size = max(size, off + sz)
    wname = os.path.basename(idx[52:180].split(b'\0')[0].decode())
    with open(os.path.join(dst, wname), 'wb') as f: f.truncate(size)
    for x in extra_files:
        with open(os.path.join(dst, x), 'wb') as f: f.write(b'x')
    sha = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
    files = {f: {'bytes': os.path.getsize(os.path.join(dst, f)), 'sha256': sha(os.path.join(dst, f)) if f != wname else '0' * 64} for f in sorted(os.listdir(dst))}
    m = {'version': 1, 'architecture': 'qwen3.5-4b', 'name': name, 'context_length': 8192, 'weights': wname, 'files': files, 'template': 'garlic-qwen35-no-thinking-v1', 'quantization': 'q4i', 'mtp': False}
    m.update(extra_manifest or {}); open(os.path.join(dst, 'bundle.json'), 'w').write(json.dumps(m, sort_keys=True, indent=2) + '\n')
    return dst, {'model.idx': files['model.idx']['sha256'], 'kernels.bin': files['kernels.bin']['sha256'], 'tokenizer.bin': files['tokenizer.bin']['sha256'], 'weights': '0' * 64}
