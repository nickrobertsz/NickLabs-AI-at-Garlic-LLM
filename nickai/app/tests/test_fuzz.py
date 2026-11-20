"""Hostile-JSON fuzzing wrapper (fuzz_api.py) + explicit regressions for bugs it found."""
import json, os, socket, subprocess, sys, tempfile, unittest, http.client
A = os.path.dirname(os.path.abspath(__file__)); HOST = os.environ.get('NATIVE_HOST_BIN', '/tmp/native-host')
@unittest.skipUnless(os.path.exists(HOST), 'host binary missing')
class Fuzz(unittest.TestCase):
    def test_mutation_fuzz_clean(self):
        for seed in ('12345', '777'):
            r = subprocess.run([sys.executable, os.path.join(A, 'fuzz_api.py'), HOST, '1200', seed], capture_output=True, text=True, timeout=300)
            rep = json.loads(r.stdout); self.assertEqual(rep['problems'], [], rep); self.assertTrue(rep['alive']); self.assertEqual(r.returncode, 0)
    def test_float_to_integer_ub_regression(self):   # found by the fuzzer: float -> long long conversion is UB (UBSan)
        p = subprocess.Popen([HOST, '0'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True); port = int(p.stdout.readline().split()[1])
        try:
            for path, body in [('/v1/chat/completions', {'model': 'fixture-a', 'max_tokens': -1e30, 'messages': [{'role': 'user', 'content': 'x'}]}),
                               ('/v1/chat/completions', {'model': 'fixture-a', 'max_completion_tokens': 1e30, 'messages': [{'role': 'user', 'content': 'x'}]}),
                               ('/v1/messages', {'model': 'fixture-a', 'max_tokens': 8, 'thinking': {'type': 'enabled', 'budget_tokens': 1e30}, 'messages': [{'role': 'user', 'content': 'x'}]}),
                               ('/v1/messages', {'model': 'fixture-a', 'max_tokens': 1e30, 'messages': [{'role': 'user', 'content': 'x'}]})]:
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=10); c.request('POST', path, json.dumps(body), {'Content-Type': 'application/json'}); r = c.getresponse(); raw = r.read(); c.close()
                self.assertEqual(r.status, 400, (path, raw)); self.assertIn(b'integer', raw)
        finally:
            p.terminate(); p.wait(10); err = p.stderr.read(); p.stdout.close(); p.stderr.close()
        self.assertNotIn('runtime error', err); self.assertNotIn('Sanitizer', err)
    def test_http_byte_fuzz_clean(self):
        for seed in ('4242', '99'):
            r = subprocess.run([sys.executable, os.path.join(A, 'fuzz_http.py'), HOST, '900', seed], capture_output=True, text=True, timeout=300)
            rep = json.loads(r.stdout); self.assertEqual(rep['problems'], [], rep); self.assertTrue(rep['alive']); self.assertEqual(r.returncode, 0)
    def test_log_injection_and_half_close_regressions(self):
        errf = tempfile.TemporaryFile('w+b'); p = subprocess.Popen([HOST, '0'], stdout=subprocess.PIPE, stderr=errf, text=True); port = int(p.stdout.readline().split()[1])
        try:
            s = socket.create_connection(('127.0.0.1', port)); s.sendall(b'GET /\x1b[31mFORGED\xff\x80 HTTP/1.1\r\nHost: x\r\n\r\n'); out = s.recv(500); s.close(); self.assertIn(b' 404 ', out[:20])
            # a client that half-closes after a valid request is treated as gone (same as Go/Ollama): the server must close quietly, never answer 500
            body = b'{"model":"fixture-a","stream":false,"messages":[{"role":"user","content":"say long"}]}'
            s = socket.create_connection(('127.0.0.1', port)); s.settimeout(10); s.sendall(b'POST /api/chat HTTP/1.1\r\nContent-Length: %d\r\n\r\n' % len(body) + body); s.shutdown(socket.SHUT_WR)
            data = b''
            while True:
                c = s.recv(4096)
                if not c: break
                data += c
            s.close(); self.assertNotIn(b' 500 ', data[:20], data[:200])
        finally:
            p.terminate(); p.wait(10); p.stdout.close(); errf.seek(0); log = errf.read(); errf.close()
        self.assertNotIn(b'\x1b', log); self.assertNotIn(b'\xff', log); self.assertIn(b'FORGED', log)   # request text reaches the log, but sanitized
if __name__ == '__main__': unittest.main()
