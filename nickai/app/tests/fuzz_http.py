#!/usr/bin/env python3
"""Byte-level fuzzing of the HTTP parser (host build, ASan/UBSan). Seeds: Slim PS4 worker's 10 framing requests (verbatim) + requests built by Claude from the
Fat worker's categories (its JSON output was truncated by a runaway path string and unusable). Each mutant is sent, the client half-closes, and the server must
answer with a well-formed status line or close, within the deadline; the server must stay alive and sanitizer-clean.
Usage: fuzz_http.py <native-host> [iterations] [seed]"""
import json, random, signal, socket, subprocess, sys, tempfile, time
BIN = sys.argv[1] if len(sys.argv) > 1 else '/tmp/native-host'; N = int(sys.argv[2]) if len(sys.argv) > 2 else 2500; SEED = int(sys.argv[3]) if len(sys.argv) > 3 else 4242
BODY = b'{"model":"fixture-a","stream":false,"messages":[{"role":"user","content":"hi"}],"options":{"num_predict":4}}'
SLIM = ["POST /path HTTP/1.1\r\nHost: example.com\r\nContent-Length: 0\r\n\r\n", "POST /path HTTP/1.1\r\nHost: example.com\r\nContent-Length: 10\r\n\r\nextra bytes", "POST /path HTTP/1.1\r\nHost: example.com\r\nContent-Length: 5\r\n\r\nextra bytes",
        "POST /path HTTP/1.1\r\nHost: example.com\r\nContent-Length: +5\r\n\r\nbody", "POST /path HTTP/1.1\r\nHost: example.com\r\nContent-Length:  5  \r\n\r\nbody", "POST /path HTTP/1.1\r\nHost: example.com\r\nCONTENT-LENGTH: 5\r\n\r\nbody",
        "POST /path HTTP/1.1\r\nHost: example.com\r\ncontent-Length: 5\r\n\r\nbody", "POST /path HTTP/1.1\r\nHost: example.com\r\nContent-Length: 0\r\nTransfer-Encoding: identity\r\n\r\nbody", "POST /path HTTP/1.1\nHost: example.com\nContent-Length: 5\n\nbody"]
SEEDS = [s.encode() for s in SLIM] + [
    b'GET /api/version HTTP/1.1\r\nHost: x\r\n\r\n', b'POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n' % len(BODY) + BODY,
    b'GET /api/tags HTTP/1.1\r\n' + b''.join(b'X-Custom: v%d\r\n' % i for i in range(60)) + b'\r\n', b'GET /api/tags HTTP/1.1\r\nHost: x\r\nX-Empty:\r\n\r\n', b'GET /api/tags HTTP/1.1\r\nHost: x\r\nX-Space : v\r\n\r\n',
    b'GET /api/tags HTTP/1.1\r\nHost: x\r\nX-Folded: a\r\n b\r\n\r\n', b'GET  /api/tags   HTTP/1.1\r\nHost: x\r\n\r\n', b'GET /api/tags HTTP/1.0\r\n\r\n', b'GET /api/tags?a=%00%ff&b=1#frag HTTP/1.1\r\nHost: x\r\n\r\n',
    b'POST /api/chat HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: %d\r\n\r\n' % len(BODY) + BODY, b'GET /' + b'a' * 5000 + b' HTTP/1.1\r\n\r\n', b'\r\n\r\n',
    b'GET /api/version HTTP/1.1\r\nHost: x\r\n\r\nGET /api/tags HTTP/1.1\r\nHost: x\r\n\r\n', b'OPTIONS * HTTP/1.1\r\nHost: x\r\n\r\n', b'HEAD / HTTP/1.1\r\n\r\n',
    b'POST /v1/messages HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}', b'POST /native/config HTTP/1.1\r\nContent-Length: 17\r\n\r\n{"max_queue":4}\r\n\r\n']
BYTES = [b'\r', b'\n', b'\r\n', b'\r\n\r\n', b' ', b':', b'\x00', b'\xff', b'\x80', b'%', b'?', b'/', b'HTTP/1.1', b'Content-Length: ', b'99999999999', b'-1', b'0', b'chunked', b'\t', b'A' * 300, b'\r\n' * 50]
OKSTATUS = {100, 200, 204, 400, 404, 405, 408, 409, 413, 422, 501, 503, 499}

def mutate(rnd, s):
    b = bytearray(s)
    for _ in range(rnd.choice([1, 1, 2, 3, 5])):
        op = rnd.choice(['flip', 'del', 'ins', 'trunc', 'dup', 'swap'])
        i = rnd.randrange(len(b) + 1) if b else 0
        if op == 'flip' and b: b[min(i, len(b) - 1)] = rnd.randrange(256)
        elif op == 'del' and b: del b[min(i, len(b) - 1):min(i, len(b) - 1) + rnd.choice([1, 1, 5, 40])]
        elif op == 'ins': b[i:i] = rnd.choice(BYTES)
        elif op == 'trunc' and b: del b[i:]
        elif op == 'dup' and b: j = min(i, len(b) - 1); b[j:j] = b[j:j + rnd.choice([1, 10, 100])]
        elif op == 'swap' and len(b) > 2: j = rnd.randrange(len(b) - 1); b[j], b[j + 1] = b[j + 1], b[j]
    return bytes(b)

def exchange(port, data, deadline=8.0):
    s = socket.create_connection(('127.0.0.1', port), timeout=deadline); s.settimeout(deadline)
    try:
        try: s.sendall(data); s.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError): pass
        out = b''; t0 = time.monotonic()
        while time.monotonic() - t0 < deadline:
            try: chunk = s.recv(65536)
            except (ConnectionResetError, socket.timeout) as e:
                if isinstance(e, socket.timeout): return out, 'timeout'
                break
            if not chunk: break
            out += chunk
        else: return out, 'timeout'
        return out, 'closed'
    finally: s.close()

def main():
    errf = tempfile.TemporaryFile('w+', errors='replace'); p = subprocess.Popen([BIN, '0'], stdout=subprocess.PIPE, stderr=errf, text=True); port = int(p.stdout.readline().split()[1])
    rnd = random.Random(SEED); problems = []; stat = {}
    for i in range(N + len(SEEDS)):
        data = SEEDS[i] if i < len(SEEDS) else mutate(rnd, rnd.choice(SEEDS))
        try: out, how = exchange(port, data)
        except Exception as e: problems.append(f'#{i} exchange error {type(e).__name__}: {e} for {data[:120]!r}'); continue
        if how == 'timeout': problems.append(f'#{i} HANG (no close within deadline) for {data[:160]!r}')
        if out:
            if not out.startswith(b'HTTP/1.1 '): problems.append(f'#{i} malformed response {out[:60]!r} for {data[:120]!r}')
            else:
                import re
                codes = [int(m) for m in re.findall(rb'HTTP/1\.1 (\d{3})', out)]   # interim 100 Continue may precede the final status
                st = codes[-1] if codes else -1
                stat[st] = stat.get(st, 0) + 1
                if st not in OKSTATUS: problems.append(f'#{i} status {st} for {data[:160]!r}')
        else: stat['no-reply'] = stat.get('no-reply', 0) + 1
        if p.poll() is not None: problems.append(f'SERVER DIED at #{i} for {data[:200]!r}'); break
        if len(problems) >= 10: break
    alive = p.poll() is None
    if alive:
        try: out, _ = exchange(port, b'GET /api/version HTTP/1.1\r\nHost: x\r\n\r\n'); alive = b' 200 ' in out[:20]
        except Exception as e: alive = False; problems.append(f'liveness: {e}')
        p.send_signal(signal.SIGTERM)
    try: p.wait(25)
    except subprocess.TimeoutExpired: p.kill(); problems.append('did not exit on SIGTERM (leaked connection threads?)')
    errf.seek(0); err = errf.read(); errf.close(); p.stdout.close(); open('/tmp/fuzz_http_stderr.txt', 'w').write(err)
    if 'Sanitizer' in err or 'runtime error' in err: problems.append('SANITIZER: ' + err[max(err.find('Sanitizer') - 20, 0):][:1500])
    print(json.dumps({'sent': N + len(SEEDS), 'statuses': stat, 'alive': alive, 'rc': p.returncode, 'problems': problems[:10]}, indent=2)); return 1 if problems or not alive else 0
if __name__ == '__main__': sys.exit(main())
