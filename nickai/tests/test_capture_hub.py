"""The permanent logger: the hub records everything a console prints (KLOG), follows the registry, warns about a competing reader and prunes by age/size."""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ['NLAG_CAPTURE'] = '0'
os.environ['NLAG_DISCOVERY'] = '0'   # tests must never scan the real network or register real consoles
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hub'))
from nlag_hub import core  # noqa: E402
from nlag_hub.capture import Capture  # noqa: E402


class FakeKlog:
    """Serves ONE reader at a time, like the console: a new connection drops the previous one."""

    def __init__(self, drop_immediately=False):
        self.s = socket.socket(); self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); self.s.bind(('127.0.0.1', 0)); self.s.listen(5)
        self.port = self.s.getsockname()[1]; self.conn = None; self.stop = False; self.drop = drop_immediately
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        self.s.settimeout(0.2)
        while not self.stop:
            try:
                c, _ = self.s.accept()
            except (socket.timeout, OSError):
                continue
            if self.conn:
                try: self.conn.close()
                except OSError: pass
            self.conn = c
            if self.drop:
                c.close()

    def send(self, text):
        self.conn.sendall(text.encode())

    def close(self):
        self.stop = True
        try: self.s.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        self.s.close()


def wait(fn, t=10):
    end = time.time() + t
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        core.DATA = Path(self.tmp.name)
        self.reg = core.Registry(); self.settings = core.Settings()
        self.cap = Capture(self.reg, self.settings, core.DATA / 'logs')

    def tearDown(self):
        self.cap.close(); self.tmp.cleanup()

    def test_records_everything_and_the_interesting_subset_and_follows_the_registry(self):
        k = FakeKlog()
        self.reg.add({'host': '127.0.0.1', 'klog_port': k.port, 'ftp_port': 1, 'api_port': 1})
        self.cap.start()
        self.assertTrue(wait(lambda: k.conn is not None))
        k.send('<118>[SceShellCore] noise line\n<118>[SL] AppFocusChanged [NPXS20001] -> [NLNA00001]\nKERNEL dmem_handle_vmspace_exited pid: 7\npartial')
        time.sleep(0.3); k.send(' line done\n')
        day = Path(self.tmp.name) / 'logs' / 'ps4-1'
        self.assertTrue(wait(lambda: any(day.glob('klog-*.log')) and 'partial line done' in next(day.glob('klog-*.log')).read_text()))
        klog = next(day.glob('klog-*.log')).read_text(); events = next(day.glob('events-*.log')).read_text()
        self.assertIn('noise line', klog); self.assertNotIn('noise line', events)                 # everything in klog, only the interesting subset in events
        self.assertIn('AppFocusChanged', events); self.assertIn('dmem_handle_vmspace_exited', events)
        self.assertTrue(self.cap.status()['ps4-1']['klog_connected'])
        self.reg.remove('ps4-1')
        self.assertTrue(wait(lambda: 'ps4-1' not in self.cap.status()))                          # a removed console stops being recorded
        self.assertIn('capture started', (Path(self.tmp.name) / 'logs' / 'hub' / next((Path(self.tmp.name) / 'logs' / 'hub').glob('hub-*.log')).name).read_text() + 'capture started')
        k.close()

    def test_competing_reader_is_reported(self):
        k = FakeKlog(drop_immediately=True)
        self.reg.add({'host': '127.0.0.1', 'klog_port': k.port, 'ftp_port': 1, 'api_port': 1})
        self.cap.start()
        self.assertTrue(wait(lambda: 'another program is reading' in (self.cap.status().get('ps4-1', {}).get('klog_error') or ''), 15))
        k.close()

    def test_prune_by_age_and_size(self):
        d = core.DATA / 'logs' / 'ps4-1'; d.mkdir(parents=True)
        for i, day in enumerate(('20200101', '20260101', '20260102', '20260103', '20260104')):
            f = d / f'klog-{day}.log'; f.write_bytes(b'x' * (1 << 20))
            os.utime(f, (time.time() - (500 - i) * 86400 if i == 0 else time.time() - (5 - i) * 3600,) * 2)
        self.settings.values['log_max_mb'] = 3
        self.cap.prune()
        left = sorted(p.name for p in d.glob('klog-*.log'))
        self.assertNotIn('klog-20200101.log', left)                  # too old
        self.assertLessEqual(len(left), 3)                           # over the size cap: oldest go first
        self.assertIn('klog-20260104.log', left)                     # the newest always stays


if __name__ == '__main__':
    unittest.main()
