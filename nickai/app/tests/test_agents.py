"""Agents on one console: requests of different agents are admitted round-robin, never concurrently; the status shows who speaks and who waits."""
import json, os, subprocess, threading, time, unittest, urllib.request
HOST = os.environ.get('NATIVE_HOST_BIN', '/tmp/native-host')


@unittest.skipUnless(os.path.exists(HOST), 'native-host missing')
class Agents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = subprocess.Popen([HOST, '0'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True); cls.port = int(cls.p.stdout.readline().split()[1])
    @classmethod
    def tearDownClass(cls): cls.p.terminate(); cls.p.wait(10); cls.p.stdout.close()

    def call(self, path, body=None, headers=None, timeout=60):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=json.dumps(body).encode() if body is not None else None, headers={'Content-Type': 'application/json', **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read() or b'null')

    def chat(self, agent, text, via='header', out=None):
        body = {'model': 'fixture-a', 'messages': [{'role': 'user', 'content': text}], 'stream': False}; headers = {}
        if via == 'header': headers['X-Agent-Id'] = agent
        elif via == 'user': body['user'] = agent
        r = self.call('/v1/chat/completions', body, headers) if via != 'ollama' else self.call('/api/chat', body, headers)
        if out is not None: out.append(agent)
        return r

    def test_round_robin_and_status(self):
        order = []; self.chat('warm', 'warm up')   # load the model first
        t1 = threading.Thread(target=self.chat, args=('alice', 'say long', 'header', order)); t1.start(); time.sleep(0.15)   # holds the GPU
        ts = []
        for agent in ('alice', 'alice', 'bob', 'carol'):   # queue behind it: alice twice, then bob and carol once
            t = threading.Thread(target=self.chat, args=(agent, 'say long', 'header' if agent != 'carol' else 'user', order)); t.start(); ts.append(t); time.sleep(0.06)
        st = self.call('/native/status')['agents']
        self.assertEqual(st['speaking'], 'alice'); self.assertEqual(st['queued'], {'alice': 2, 'bob': 1, 'carol': 1})
        for t in [t1] + ts: t.join(120)
        self.assertEqual(order[0], 'alice')
        self.assertEqual(sorted(order[1:3]), ['bob', 'carol'])   # the agents that had not spoken go before alice's second request
        self.assertEqual(order[3:], ['alice', 'alice'])
        self.assertEqual(self.call('/native/status')['agents'], {'speaking': '', 'queued': {}})
        log = '\n'.join(self.call('/native/log?n=100')['log']); self.assertIn('agent=bob', log); self.assertIn('agent=carol', log)

    def test_agent_name_is_sanitized(self):
        self.chat('a b/../<x>' + 'z' * 100, 'hi'); log = '\n'.join(self.call('/native/log?n=20')['log'])
        self.assertIn('agent=ab..x' + 'z' * 59, log)   # only [A-Za-z0-9._:@-], at most 64 characters


if __name__ == '__main__':
    unittest.main()
