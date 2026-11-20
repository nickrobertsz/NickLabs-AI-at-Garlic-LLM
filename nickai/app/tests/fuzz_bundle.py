#!/usr/bin/env python3
"""Fuzz the bundle validators (tokenizer / kernels / model index / mtp index) with mutations of the REAL files, under ASan/UBSan. They parse binary data on the
load path before anything reaches the GPU: a crash or hang there would kill the console app at load. Property: every input is answered OK or REJECT, never a
crash/hang; the unmutated files are OK. (Mutation helpers: put_u32le/u32le_at from the Slim PS4 worker with the masking fix; the Fat worker's mutate_bytes did not
parse and was rewritten.)  Usage: fuzz_bundle.py <bundle dir> [seed]"""
import os, random, subprocess, sys
BUNDLE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'ps4-worker/build/bundle-9b'); SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 5
DRV = os.environ.get('VALIDATE_DRIVER', '/tmp/validate_driver'); WEIGHTS = 4641752088
INTERESTING = [0, 1, 2, 0x7fffffff, 0x80000000, 0xffffffff, 0x10000, 0x100000, 255, 256]

def put_u32le(data, offset, value):
    if not (0 <= offset and offset + 4 <= len(data)): return data
    return data[:offset] + (value & 0xFFFFFFFF).to_bytes(4, 'little') + data[offset + 4:]
def u32le_at(data, offset):
    return int.from_bytes(data[offset:offset + 4], 'little') if 0 <= offset and offset + 4 <= len(data) else None
def mutate_bytes(data, rnd, structured_prefix=0):
    b = bytearray(data)
    for _ in range(rnd.randint(1, 4)):
        op = rnd.choice(['set', 'bit', 'del', 'ins', 'trunc', 'u32', 'u32', 'u32'] if b else ['ins'])
        hi = len(b) if not structured_prefix or rnd.random() < 0.3 else min(len(b), structured_prefix)   # mostly hit the header/index area where the parsers read sizes
        if op == 'set': b[rnd.randrange(hi)] = rnd.randrange(256)
        elif op == 'bit': b[rnd.randrange(hi)] ^= 1 << rnd.randrange(8)
        elif op == 'del': i = rnd.randrange(hi); del b[i:i + rnd.randint(1, 16)]
        elif op == 'ins': i = rnd.randint(0, len(b)); b[i:i] = bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 16)))
        elif op == 'trunc': del b[rnd.randrange(hi + 1):]
        else:
            if len(b) >= 4: b = bytearray(put_u32le(bytes(b), rnd.randrange(max(1, hi - 3)), rnd.choice(INTERESTING + [len(b)])))
    return bytes(b)
def main():
    if not os.path.exists(DRV): sys.exit('build tests/validate_driver.cpp first (see tools/test.sh)')
    files = {'tok': 'tokenizer.bin', 'krn': 'kernels.bin', 'idx': 'model.idx', 'mtp': 'mtp.idx'}; data = {k: open(os.path.join(BUNDLE, f), 'rb').read() for k, f in files.items()}
    plan = [('idx', 3000, 224 + 36 * 8), ('mtp', 3000, 200), ('krn', 1500, 200), ('tok', 150, 1100)]   # (kind, iterations, header area to favour)
    p = subprocess.Popen([DRV], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE); rnd = random.Random(SEED); stats = {}; problems = []
    def ask(kind, blob):
        p.stdin.write(f'{kind} {len(blob)} {WEIGHTS}\n'.encode() + blob); p.stdin.flush(); line = p.stdout.readline().decode().strip()
        if not line: raise RuntimeError('driver died (crash/abort)')
        return line
    try:
        for kind, _, _ in plan:
            r = ask(kind, data[kind])
            if r != 'OK': problems.append(f'unmutated {files[kind]} rejected: {r}')
        for kind, n, area in plan:
            for i in range(n):
                blob = mutate_bytes(data[kind], rnd, area); r = ask(kind, blob); key = (kind, r.split(' ')[0]); stats[key] = stats.get(key, 0) + 1
                if r not in ('OK',) and not r.startswith('REJECT'): problems.append(f'{kind} #{i}: {r}')
    except Exception as e:
        err = p.stderr.read().decode(errors='replace')[:1500] if p.poll() is not None else ''; problems.append(f'{type(e).__name__}: {e} {err}')
    finally:
        try: p.stdin.close()
        except Exception: pass
        try: p.wait(15)
        except subprocess.TimeoutExpired: p.kill(); problems.append('driver did not exit')
    err = p.stderr.read().decode(errors='replace')
    if 'Sanitizer' in err or 'runtime error' in err: problems.append('SANITIZER: ' + err[:1500])
    print({f'{k}:{v}': n for (k, v), n in sorted(stats.items())}); print('PROBLEMS:' if problems else 'CLEAN', *problems, sep='\n'); return 1 if problems else 0
if __name__ == '__main__': sys.exit(main())
