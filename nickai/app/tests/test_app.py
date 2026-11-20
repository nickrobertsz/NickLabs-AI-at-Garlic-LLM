"""The REAL console main.cpp + garlic_backend.cpp on the host with a scripted mock engine: startup, autoload, persisted config/state,
model swap/release (relaunch is observed through a marker file on the host), models.json, fence watchdog, restart limits. No GPU."""
import hashlib, json, os, shutil, signal, subprocess, sys, tempfile, threading, time, unittest, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fixtures import free_port, make_bundle_variant, make_bundle_4b, wait_until
A = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); R = os.path.dirname(A)
APP = os.environ.get('NATIVE_APP_HOST_BIN', '/tmp/native-app-host'); BUNDLE = os.path.join(R, 'ps4-worker/build/bundle-9b'); TRUSTED = os.path.join(R, 'ps4-worker/trusted-assets.json')

def bundle_digest(d): return hashlib.sha256(open(os.path.join(d, 'bundle.json'), 'rb').read()).hexdigest()
def bundle_size(d): return sum(v['bytes'] for v in json.load(open(os.path.join(d, 'bundle.json')))['files'].values())   # the app's definition: manifest bytes

@unittest.skipUnless(os.path.exists(APP) and os.path.isdir(BUNDLE), 'app host binary or real bundle missing')
class App(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(); self.port = free_port(); self.procs = []; self.second = os.path.join(self.dir, 'second')
        make_bundle_variant(BUNDLE, self.second, 'second-model')
        # the 4.6 GB weights are never hashed on the host: pre-seed the verified cache exactly as the app would have written it
        self.verified = {'sha256:' + bundle_digest(d): {'size': bundle_size(d)} for d in (BUNDLE, self.second)}
        json.dump(self.verified, open(os.path.join(self.dir, 'verified.json'), 'w'))
        json.dump({'bundles': [self.second]}, open(os.path.join(self.dir, 'models.json'), 'w'))
        self.cfg({'port': self.port, 'model_dirs': [BUNDLE], 'queue_wait_s': 30})
    def tearDown(self):
        for p in self.procs: self.stop(p)
        shutil.rmtree(self.dir, ignore_errors=True)
    def cfg(self, d, name='config.json'): json.dump(d, open(os.path.join(self.dir, name), 'w'))
    def start(self, **env):
        self.errf = open(os.path.join(self.dir, 'stderr.txt'), 'w')
        e = {**os.environ, 'NATIVE_DATA_DIR': self.dir, 'NATIVE_TRUSTED': getattr(self, 'trusted', TRUSTED), 'NATIVE_WATCHDOG_MS': '150', 'NATIVE_RELAUNCH_BACKOFF_MS': '300', 'ASAN_OPTIONS': 'detect_leaks=0', **env}
        p = subprocess.Popen([APP], env=e, stdout=subprocess.DEVNULL, stderr=self.errf); self.procs.append(p)
        wait_until(lambda: self.get('/api/version'), 20, 0.1, 'server up'); return p
    def stop(self, p):
        if p.poll() is None: p.send_signal(signal.SIGKILL)
        p.wait(10)
    def call(self, method, path, body=None, timeout=60):
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=json.dumps(body).encode() if body is not None else None, method=method, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as x: return x.status, json.loads(x.read() or b'null')
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b'null')
    def get(self, path): s, b = self.call('GET', path); return b if s == 200 else None
    def ps(self): return [m['name'] for m in (self.get('/api/ps') or {'models': []})['models']]
    def chat(self, model='qwen3.5-9b-garlic', content='hello'): return self.call('POST', '/api/chat', {'model': model, 'stream': False, 'messages': [{'role': 'user', 'content': content}]})
    def state(self):
        p = os.path.join(self.dir, 'state.json'); return json.load(open(p)) if os.path.exists(p) else {}
    def marker(self): p = os.path.join(self.dir, 'relaunch.marker'); return open(p).read().count('relaunch requested') if os.path.exists(p) else 0
    def log(self): return open(os.path.join(self.dir, 'stderr.txt')).read()

    def four_b(self, trust=True, **kw):
        """Stage a qwen3.5-4b bundle (real shipped small files) in models.json and, unless trust=False, add its pins to the trusted list."""
        d, rec = make_bundle_4b(os.path.join(self.dir, 'fourb'), R, **kw)
        self.verified['sha256:' + bundle_digest(d)] = {'size': bundle_size(d)}; json.dump(self.verified, open(os.path.join(self.dir, 'verified.json'), 'w'))
        base = json.load(open(TRUSTED)); sets = base if isinstance(base, list) else [base]; self.trusted = os.path.join(self.dir, 'trusted.json')
        json.dump(sets + ([rec] if trust else []), open(self.trusted, 'w')); json.dump({'bundles': [self.second, d]}, open(os.path.join(self.dir, 'models.json'), 'w')); return d

    def test_01_autoload_and_chat(self):
        self.start()
        st = self.get('/native/status'); self.assertEqual(st['config']['port'], self.port); self.assertIn('qwen3.5-9b-garlic', [m['name'] for m in self.get('/api/tags')['models']])
        wait_until(lambda: self.ps() == ['qwen3.5-9b-garlic'], 60, 0.2, 'autoload')       # server is up BEFORE the model finished loading
        s, b = self.chat(); self.assertEqual(s, 200, b); self.assertEqual(b['message']['content'], 'mock reply')
        self.assertEqual(self.ps(), ['qwen3.5-9b-garlic']); self.assertEqual(self.get('/api/ps')['models'][0]['context_length'], 16384)
        self.assertEqual(self.state()['desired_model'], 'qwen3.5-9b-garlic'); self.assertEqual(self.state()['restarts_since_ok'], 0)
        self.assertIn('data_dir_writable=yes', self.log())
    def test_02_requests_during_load_wait_then_succeed(self):
        self.start(); s, b = self.chat()     # may arrive while the autoload holds the GPU gate: it queues, then runs
        self.assertEqual((s, b['message']['content']), (200, 'mock reply'))
    def test_03_config_persists_across_restart_and_ctx_ceiling(self):
        p = self.start(MOCK_CTX='4096'); wait_until(lambda: self.ps(), 60, 0.2, 'load')
        self.assertEqual(self.get('/api/ps')['models'][0]['context_length'], 4096)    # engine ceiling reported, not 16K
        s, b = self.call('POST', '/native/config', {'default_num_ctx': 2048, 'default_think': True, 'max_queue': 3}); self.assertEqual(s, 200, b)
        self.assertEqual(json.load(open(os.path.join(self.dir, 'config.local.json')))['default_num_ctx'], 2048)
        self.stop(p); self.start(MOCK_CTX='4096'); cfg = self.get('/native/config')
        self.assertEqual((cfg['default_num_ctx'], cfg['default_think'], cfg['max_queue'], cfg['port']), (2048, True, 3, self.port))   # local overrides layered over config.json
        self.assertEqual(self.call('POST', '/native/config', {'port': 70000})[0], 400); self.assertEqual(self.call('POST', '/native/config', {'data_dir': '/tmp/x'})[0], 400)
    def test_04_release_then_idle_restart(self):
        p = self.start(); wait_until(lambda: self.ps(), 60, 0.2, 'load')
        self.assertEqual(self.call('POST', '/native/unload', {'release': True})[0], 409)             # needs allow_release_restart
        self.call('POST', '/native/config', {'allow_release_restart': True})
        s, b = self.call('POST', '/native/unload', {'release': True}); self.assertEqual((s, b['status']), (202, 'restarting'))
        self.assertEqual(self.state()['desired_model'], ''); wait_until(lambda: self.marker() >= 1, 10, 0.1, 'relaunch request')
        self.stop(p); self.start(); time.sleep(1.5)
        self.assertEqual(self.ps(), []); self.assertIn('idle: no model loaded at startup', self.log())    # explicit release survives the relaunch: no autoload
    def test_05_swap_requires_relaunch_and_persists_choice(self):
        p = self.start(); wait_until(lambda: self.ps() == ['qwen3.5-9b-garlic'], 60, 0.2, 'load')
        self.assertIn('second-model', [m['name'] for m in self.get('/api/tags')['models']])            # discovered via models.json (no dirent on the console)
        s, b = self.chat('second-model'); self.assertEqual(s, 503); self.assertEqual(b['code'], 'swapping')
        st = self.state(); self.assertEqual((st['desired_model'], st.get('pending_swap')), ('second-model', True)); wait_until(lambda: self.marker() >= 1, 10, 0.1, 'relaunch request')
        s, b = self.chat('qwen3.5-9b-garlic'); self.assertIn(s, (200, 503))                           # while "restarting" requests are refused cleanly, never hang
        self.stop(p); self.start(); wait_until(lambda: self.ps() == ['second-model'], 60, 0.2, 'autoload of the persisted choice')
        s, b = self.chat('second-model'); self.assertEqual((s, b['message']['content']), (200, 'mock reply'))
        self.assertNotIn('pending_swap', self.state())
    def test_06_fence_watchdog_relaunches_at_most_three_times(self):
        fault = os.path.join(self.dir, 'FAULT'); self.start(MOCK_FAULT_FILE=fault); wait_until(lambda: self.ps(), 60, 0.2, 'load')
        self.assertEqual(self.chat()[0], 200); open(fault, 'w').close()
        s, b = self.chat(); self.assertEqual((s, b['code']), (500, 'gpu_fault'))
        self.assertTrue(self.get('/native/status')['backend']['gpu_fenced'])
        s, b = self.chat(); self.assertEqual((s, b['code']), (503, 'gpu_fenced'))                      # loud, immediate refusal
        wait_until(lambda: self.marker() >= 3 and self.state().get('restarts_since_ok') == 3, 20, 0.1, 'three automatic relaunch requests')
        time.sleep(1.5); self.assertEqual(self.marker(), 3, 'must stop after 3'); self.assertIn('3 automatic relaunches already failed', self.log())
        self.assertEqual(self.get('/native/health')['ok'], True)                                       # the API itself stays up and reports
    def test_07_corrupt_state_and_config_are_survivable(self):
        open(os.path.join(self.dir, 'state.json'), 'w').write('{not json'); open(os.path.join(self.dir, 'config.local.json'), 'w').write('garbage')
        self.start(); wait_until(lambda: self.ps() == ['qwen3.5-9b-garlic'], 60, 0.2, 'autoload despite corrupt files'); self.assertEqual(self.chat()[0], 200)
        self.assertIn('config ignored', self.log())
    def test_08_missing_trusted_assets_fails_closed(self):
        p = subprocess.Popen([APP], env={**os.environ, 'NATIVE_DATA_DIR': self.dir, 'NATIVE_TRUSTED': os.path.join(self.dir, 'nope.json')}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.procs.append(p); p.wait(15); self.assertEqual(p.returncode, 1); self.assertIn('trusted-assets.json missing', p.stderr.read()); p.stderr.close()
    def test_09_tampered_bundle_rejected_server_stays_up(self):
        open(os.path.join(self.second, 'tokenizer.bin'), 'r+b').write(b'X' * 16)                       # corrupt a pinned file in the second bundle
        self.start(); wait_until(lambda: self.ps(), 60, 0.2, 'first model')
        s, b = self.chat('second-model'); self.assertEqual((s, b['code']), (422, 'bundle_invalid')); self.assertEqual(self.ps(), ['qwen3.5-9b-garlic'])   # refused, resident model unaffected
        self.assertEqual(self.chat()[0], 200); self.assertNotIn('pending_swap', self.state())
    def test_10_startup_falls_back_to_last_good_model(self):
        open(os.path.join(self.second, 'tokenizer.bin'), 'r+b').write(b'X' * 16)                       # the persisted choice is broken...
        json.dump({'desired_model': 'second-model', 'last_good_model': 'qwen3.5-9b-garlic', 'pending_swap': True}, open(os.path.join(self.dir, 'state.json'), 'w'))
        self.start(); wait_until(lambda: self.ps() == ['qwen3.5-9b-garlic'], 60, 0.2, 'fallback to last known-good')   # ...so the app comes up on the last model that worked
        self.assertIn('falling back to last known-good model', self.log()); self.assertEqual(self.chat()[0], 200); self.assertNotIn('pending_swap', self.state())
    def test_11_swap_prevalidation_leaves_state_untouched(self):
        open(os.path.join(self.second, 'tokenizer.bin'), 'r+b').write(b'X' * 16); self.start(); wait_until(lambda: self.ps(), 60, 0.2, 'load')
        before = self.state(); s, b = self.chat('second-model'); self.assertEqual(s, 422); self.assertEqual(self.marker(), 0)   # no relaunch was requested
        self.assertEqual(self.state().get('desired_model'), before.get('desired_model')); self.assertNotIn('pending_swap', self.state())
    # ---- the second architecture: qwen3.5-4b (no MTP head: 4 files, plain one-token decode)
    def test_12_four_b_without_mtp_loads_and_decodes(self):
        self.four_b(); self.cfg({'port': self.port, 'model_dirs': [], 'queue_wait_s': 30}); json.dump({'desired_model': 'qwen3.5-4b-garlic'}, open(os.path.join(self.dir, 'state.json'), 'w'))
        self.start(); wait_until(lambda: self.ps() == ['qwen3.5-4b-garlic'], 60, 0.2, '4B autoload')     # spec_init is refused by the mock for a bundle without mtp.idx: it must never be called
        s, b = self.chat('qwen3.5-4b-garlic'); self.assertEqual((s, b['message']['content']), (200, 'mock reply'))     # multi-token reply via the plain engine_run decode loop
        self.assertIs(self.get('/native/status')['backend']['mtp'], False)
        tags = {m['name']: m for m in self.get('/api/tags')['models']}; self.assertEqual(tags['qwen3.5-4b-garlic']['details']['parameter_size'], '4B'); self.assertEqual(tags['qwen3.5-4b-garlic']['details']['quantization_level'], 'Q4_I')
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}/api/chat', data=json.dumps({'model': 'qwen3.5-4b-garlic', 'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]}).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(r, timeout=60) as x: chunks = [json.loads(l) for l in x.read().splitlines() if l.strip()]       # NDJSON stream
        self.assertEqual(''.join(c.get('message', {}).get('content', '') for c in chunks), 'mock reply'); self.assertIs(chunks[-1]['done'], True)
    def test_13_swap_between_9b_and_4b_both_ways(self):
        self.four_b(); p = self.start(); wait_until(lambda: self.ps() == ['qwen3.5-9b-garlic'], 60, 0.2, '9B load'); self.assertIs(self.get('/native/status')['backend']['mtp'], True)
        s, b = self.chat('qwen3.5-4b-garlic'); self.assertEqual((s, b['code']), (503, 'swapping')); wait_until(lambda: self.marker() >= 1, 10, 0.1, 'relaunch request')
        self.assertEqual(self.state()['desired_model'], 'qwen3.5-4b-garlic'); self.stop(p); p = self.start(); wait_until(lambda: self.ps() == ['qwen3.5-4b-garlic'], 60, 0.2, '4B after relaunch')
        s, b = self.chat('qwen3.5-4b-garlic'); self.assertEqual((s, b['message']['content']), (200, 'mock reply')); self.assertIs(self.get('/native/status')['backend']['mtp'], False)
        s, b = self.chat('qwen3.5-9b-garlic'); self.assertEqual((s, b['code']), (503, 'swapping')); self.stop(p); self.start(); wait_until(lambda: self.ps() == ['qwen3.5-9b-garlic'], 60, 0.2, '9B again')   # reload the first model
        s, b = self.chat('qwen3.5-9b-garlic'); self.assertEqual((s, b['message']['content']), (200, 'mock reply')); self.assertIs(self.get('/native/status')['backend']['mtp'], True)
    def test_14_four_b_pins_and_structure_are_enforced(self):
        self.four_b(trust=False); self.start(); wait_until(lambda: self.ps(), 60, 0.2, 'load')
        s, b = self.chat('qwen3.5-4b-garlic'); self.assertEqual((s, b['code']), (422, 'bundle_invalid')); self.assertIn('trusted asset list', b['error']['message'] if isinstance(b.get('error'), dict) else str(b))   # no trust record = refused, no relaunch
        self.assertEqual(self.marker(), 0); self.assertEqual(self.ps(), ['qwen3.5-9b-garlic'])
        for kw, why in (({'extra_manifest': {'mtp_weights': 'qwen3.5-9b-mtp.bin'}}, 'an MTP weights entry on a 4B'), ({'extra_files': ('extra.bin',)}, 'a fifth file')):
            shutil.rmtree(os.path.join(self.dir, 'fourb'), ignore_errors=True); self.four_b(**kw); self.stop(self.procs[-1]); self.start(); wait_until(lambda: self.ps(), 60, 0.2, 'load')
            s, b = self.chat('qwen3.5-4b-garlic'); self.assertEqual((s, b['code']), (422, 'bundle_invalid'), why); self.assertEqual(self.marker(), 0)
    def test_15_hardware_swap_driver_runs_a_to_e_with_real_relaunches(self):
        """tools/swap_test.py (the operator's A-E sequence) against the real app: every 'release' makes the app ask for a relaunch, and a supervisor plays the
        console's LoadExec (old process ends, a new one starts). Proves the driver's logic before it is spent on hardware."""
        sys.path.insert(0, os.path.join(A, 'tools')); import swap_test
        self.four_b(); cur = [self.start()]; seen = [0]; done = threading.Event()
        def supervise():
            while not done.is_set():
                n = self.marker()
                if n > seen[0]:
                    seen[0] = n; time.sleep(0.4); self.stop(cur[0]); cur[0] = self.start()
                time.sleep(0.1)
        th = threading.Thread(target=supervise, daemon=True); th.start(); lines = []
        try:
            run = swap_test.Run(swap_test.Api(f'http://127.0.0.1:{self.port}'), 'qwen3.5-9b-garlic', 'qwen3.5-4b-garlic', 'mock reply', log=lines.append, load_timeout=120, restart_timeout=60); ok = run.go()
        finally: done.set(); th.join(10)
        self.assertTrue(ok, '\n'.join(lines)); names = [x['step'] for x in run.steps]
        for want in ('A1 load model 1', 'A2 inference on model 1', 'B unload model 1 (release)', 'C1 load model 2', 'C2 inference on model 2', 'D unload model 2 (release)', 'E1 reload model 1', 'E2 inference on model 1 again', 'F memory fully returned'): self.assertIn(want, names)
        self.assertGreaterEqual(self.marker(), 3)                                                  # preflight release + B + D
        ups = [x for x in run.steps if 'restart_delta_s' in x]; self.assertEqual(len(ups), 3); self.assertTrue(all(x['restart_delta_s'] > 0.5 for x in ups))     # each release was a NEW process
        md = swap_test.render('fat', run, ok, 'x'); self.assertIn('**Verdict:** PASS', md); self.assertIn('F memory fully returned', md)
    def test_16_swap_driver_reports_failure_honestly(self):
        """If the second model cannot be loaded the driver stops at that step and says NOT PASSED (no fabricated success)."""
        sys.path.insert(0, os.path.join(A, 'tools')); import swap_test
        self.four_b(trust=False); self.start(); lines = []
        run = swap_test.Run(swap_test.Api(f'http://127.0.0.1:{self.port}'), 'qwen3.5-9b-garlic', 'qwen3.5-4b-garlic', 'mock reply', log=lines.append, load_timeout=60, restart_timeout=20)
        self.assertFalse(run.go()); self.assertFalse(run.steps[-1]['ok']); self.assertIn('NOT PASSED', swap_test.render('fat', run, False, 'x'))
    def test_17_clock_self_check_and_start_stamp(self):
        """Hardware lesson: the console's steady_clock did not tick. The app logs its clock against the wall clock at every start, and stamps /native/status with the wall time it started."""
        t0 = time.time(); self.start(); st = self.get('/native/status')
        self.assertIsInstance(st['started_at'], float); self.assertLess(abs(st['started_at'] - t0), 15)
        h = st['http']; self.assertEqual(h['spawn_failures'], 0); self.assertEqual(h['accept_errors'], 0); self.assertGreaterEqual(h['accepted'], 1); self.assertEqual(h['max_connections'], 32)   # 00.05 connection counters
        import re
        wait_until(lambda: 'clock_check' in self.log(), 15, 0.2, 'clock check line'); m = re.search(r'clock_check wall_dt=([\d.]+) app_clock_dt=([\d.]+)', self.log())
        wall, app = float(m.group(1)), float(m.group(2)); self.assertGreater(wall, 2.5); self.assertLess(abs(wall - app), 0.3)       # the app clock tracks wall time
        time.sleep(1.2); self.assertGreater(self.get('/native/status')['uptime_s'], 1.0)

if __name__ == '__main__':
    unittest.main()
