"""Soak the host server (ASan/UBSan build): no failures, no fd/thread/RSS growth, clean sanitizer exit."""
import importlib.util, os, signal, subprocess, sys, tempfile, time, unittest
A = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); HOST = os.environ.get('NATIVE_HOST_BIN', '/tmp/native-host'); SECONDS = float(os.environ.get('SOAK_SECONDS', '12'))
def load(n):
    s = importlib.util.spec_from_file_location(n, os.path.join(A, 'tools', n + '.py')); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
@unittest.skipUnless(os.path.exists(HOST), 'native-host missing')
class Soak(unittest.TestCase):
    def test_soak_no_leaks(self):
        pm, sk = load('procmon'), load('soak')
        errf = tempfile.TemporaryFile('w+')
        p = subprocess.Popen([HOST, '0'], stdout=subprocess.PIPE, stderr=errf, text=True)
        port = int(p.stdout.readline().split()[1])   # stderr goes to a file: an undrained pipe would stall the server
        try:
            sk.soak('127.0.0.1', port, 2, 4, 'fixture-a'); time.sleep(1); before = pm.proc_stats(p.pid)   # warm-up, then baseline
            rep = sk.soak('127.0.0.1', port, SECONDS, 8, 'fixture-a'); time.sleep(3); after = pm.proc_stats(p.pid)
            fails = {k: v for k, v in rep['counts'].items() if v['fail']}
            self.assertEqual(fails, {}, rep['problems'])
            self.assertTrue(all(v['ok'] > 0 for v in rep['counts'].values()), rep['counts'])
            self.assertEqual(pm.leaked(before, after, rss_slack_kb=100000), [], (before, after))
        finally:
            p.send_signal(signal.SIGTERM)
            try: p.wait(15)
            except subprocess.TimeoutExpired: p.kill()
            errf.seek(0); err = errf.read(); p.stdout.close(); errf.close()
        self.assertEqual(p.returncode, 0, err[-1500:]); self.assertNotIn('Sanitizer', err); self.assertNotIn('LeakSanitizer', err)
    def test_procmon_units(self):
        pm = load('procmon'); s = pm.proc_stats(os.getpid()); self.assertTrue(all(isinstance(v, int) for v in s.values()), s)
        self.assertEqual(pm.proc_stats(2 ** 30), {'fds': None, 'threads': None, 'rss_kb': None})
        self.assertEqual(pm.leaked({'fds': 10, 'threads': 3, 'rss_kb': 1}, {'fds': 25, 'threads': 4, 'rss_kb': None}), ['fds grew 10 -> 25 (+15 > 4)'])
if __name__ == '__main__': unittest.main()
