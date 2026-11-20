#!/usr/bin/env python3
"""Host end-to-end tests of the native API (real HTTP, fixture backend). Usage: e2e_api.py <native-host binary>"""
import http.client, json, os, signal, subprocess, sys, tempfile, threading, time, unittest

BIN = sys.argv.pop(1) if len(sys.argv) > 1 else '/tmp/native-host'

class Host:
    def __init__(self, cfg=None):
        self.dir = tempfile.mkdtemp()
        self.cfgpath = os.path.join(self.dir, 'config.json')
        if cfg is not None:
            json.dump(cfg, open(self.cfgpath, 'w'))
        self.p = subprocess.Popen([BIN, '0', self.cfgpath], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.p.stdout.readline()
        assert line.startswith('PORT'), line
        self.port = int(line.split()[1])
    def req(self, method, path, body=None, headers=None, raw=False):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        data = body if isinstance(body, (bytes, str)) else (json.dumps(body) if body is not None else None)
        c.request(method, path, data, headers or {})
        r = c.getresponse(); b = r.read(); c.close()
        if raw: return r.status, dict(r.getheaders()), b
        try: return r.status, dict(r.getheaders()), json.loads(b) if b else None
        except ValueError: return r.status, dict(r.getheaders()), b.decode()
    def stream(self, path, body):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        c.request('POST', path, json.dumps(body)); r = c.getresponse()
        return r.status, [l for l in r.read().decode().split('\n') if l.strip()]
    def stop(self):
        self.p.send_signal(signal.SIGTERM)
        try: self.p.wait(10)
        except Exception: self.p.kill()
        err = self.p.stderr.read()
        return self.p.returncode, err

class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.h = Host()
    @classmethod
    def tearDownClass(cls):
        rc, err = cls.h.stop()
        assert rc == 0, (rc, err[-2000:])
        assert 'AddressSanitizer' not in err and 'runtime error' not in err and 'ThreadSanitizer' not in err, err[-3000:]
    def test_01_basics(self):
        s, _, b = self.h.req('GET', '/'); self.assertEqual((s, b), (200, 'Ollama is running'))
        s, _, b = self.h.req('GET', '/api/version'); self.assertIn('version', b)
        s, _, b = self.h.req('GET', '/api/tags'); self.assertEqual([m['name'] for m in b['models']], ['fixture-a', 'fixture-b'])
        s, _, b = self.h.req('POST', '/api/show', {'model': 'fixture-a'}); self.assertIn('tools', b['capabilities']); self.assertEqual(b['model_info']['qwen35.context_length'], 8192)
        s, _, b = self.h.req('POST', '/api/show', {'model': 'nope'}); self.assertEqual(s, 404)
        s, _, b = self.h.req('GET', '/v1/models'); self.assertEqual(len(b['data']), 2)
        s, _, b = self.h.req('OPTIONS', '/api/chat', raw=True); self.assertEqual(s, 204)
    def test_02_chat_nonstream_and_stream(self):
        s, h, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': 'hello world'}], 'options': {'temperature': 0.3, 'num_ctx': 99999, 'top_p': 0.9, 'top_k': 40}})
        self.assertEqual(s, 200); self.assertTrue(b['done']); self.assertIn('hello world', b['message']['content']); self.assertIn('T=0.3', b['message']['content'])
        self.assertEqual(h['X-Native-Context-Clamped'], '8192'); self.assertEqual(h['X-Native-Ignored-Options'], 'top_k,top_p'); self.assertEqual(h['X-Native-Context'], '8192')
        self.assertGreater(b['eval_count'], 0); self.assertIn('total_duration', b)
        s, lines = self.h.stream('/api/chat', {'model': 'fixture-a', 'messages': [{'role': 'user', 'content': 'stream me'}]})
        objs = [json.loads(l) for l in lines]; self.assertTrue(objs[-1]['done']); self.assertEqual(''.join(o['message']['content'] for o in objs), 'Echo (T=0.70, ctx=8192): stream me')
        self.assertGreater(len(objs), 3)
    def test_03_generate(self):
        s, _, b = self.h.req('POST', '/api/generate', {'model': 'fixture-a', 'prompt': 'abc def', 'stream': False, 'system': 'be nice'})
        self.assertIn('abc def', b['response']); self.assertTrue(b['done'])
        s, _, b = self.h.req('POST', '/api/generate', {'model': 'fixture-a', 'prompt': 'raw text', 'raw': True, 'stream': False}); self.assertIn('raw text', b['response'])
        s, _, b = self.h.req('POST', '/api/generate', {'model': 'fixture-a', 'prompt': 'x', 'images': ['aGk=']}); self.assertEqual(s, 400)
    def test_04_stop_and_length(self):
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': 'say long'}], 'options': {'stop': ['word5 ']}})
        self.assertTrue(b['message']['content'].endswith('word4 ')); self.assertNotIn('word5', b['message']['content']); self.assertEqual(b['done_reason'], 'stop')
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': 'say long'}], 'options': {'num_predict': 7}})
        self.assertEqual(b['done_reason'], 'length'); self.assertEqual(b['eval_count'], 7)
        s, lines = self.h.stream('/api/chat', {'model': 'fixture-a', 'messages': [{'role': 'user', 'content': 'say long'}], 'options': {'stop': ['word10 w']}})
        txt = ''.join(json.loads(l).get('message', {}).get('content', '') for l in lines); self.assertTrue(txt.endswith('word9 ')); self.assertNotIn('word10', txt)
    def test_05_tools(self):
        tools = [{'type': 'function', 'function': {'name': 'get_weather', 'description': 'w', 'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}}]
        msgs = [{'role': 'system', 'content': 'sys'}, {'role': 'user', 'content': 'what is the weather'}]
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': msgs, 'tools': tools})
        tc = b['message']['tool_calls']; self.assertEqual(tc[0]['function'], {'name': 'get_weather', 'arguments': {'city': 'Paris'}}); self.assertEqual(b['message']['content'], 'Let me check.')
        s, lines = self.h.stream('/api/chat', {'model': 'fixture-a', 'messages': msgs, 'tools': tools})
        objs = [json.loads(l) for l in lines]; self.assertNotIn('tool_call', ''.join(o['message']['content'] for o in objs)); self.assertTrue(any('tool_calls' in o['message'] for o in objs))
        # tool result round trip
        msgs2 = msgs + [{'role': 'assistant', 'content': '', 'tool_calls': tc}, {'role': 'tool', 'content': '21C sunny', 'tool_name': 'get_weather'}]
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': msgs2, 'tools': tools}); self.assertIn('The tool said', b['message']['content']); self.assertNotIn('tool_calls', b['message'])
    def test_06_think(self):
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'think': True, 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertIn('Reasoning', b['message']['thinking']); self.assertNotIn('Reasoning', b['message']['content'])
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': 'hi'}]}); self.assertNotIn('thinking', b['message'])
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'think': 'bogus', 'messages': [{'role': 'user', 'content': 'hi'}]}); self.assertEqual(s, 400)
    def test_07_openai(self):
        s, _, b = self.h.req('POST', '/v1/chat/completions', {'model': 'fixture-a', 'messages': [{'role': 'user', 'content': 'hey'}], 'max_tokens': 50, 'reasoning_effort': 'low'})
        self.assertEqual(b['object'], 'chat.completion'); self.assertIn('hey', b['choices'][0]['message']['content']); self.assertIn('Reasoning', b['choices'][0]['message']['reasoning_content']); self.assertGreater(b['usage']['completion_tokens'], 0)
        s, lines = self.h.stream('/v1/chat/completions', {'model': 'fixture-a', 'stream': True, 'stream_options': {'include_usage': True}, 'messages': [{'role': 'user', 'content': 'hey'}]})
        self.assertEqual(lines[-1], 'data: [DONE]'); objs = [json.loads(l[6:]) for l in lines[:-1]]
        self.assertEqual(objs[0]['choices'][0]['delta']['role'], 'assistant'); self.assertEqual(objs[-1]['choices'], []); self.assertIn('usage', objs[-1])
        self.assertEqual([o for o in objs if o['choices'] and o['choices'][0]['finish_reason']][0]['choices'][0]['finish_reason'], 'stop')
        tools = [{'type': 'function', 'function': {'name': 'get_weather', 'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}}]
        s, _, b = self.h.req('POST', '/v1/chat/completions', {'model': 'fixture-a', 'messages': [{'role': 'system', 'content': 'x'}, {'role': 'user', 'content': 'weather?'}], 'tools': tools})
        ch = b['choices'][0]; self.assertEqual(ch['finish_reason'], 'tool_calls'); self.assertEqual(json.loads(ch['message']['tool_calls'][0]['function']['arguments']), {'city': 'Paris'})
        s, lines = self.h.stream('/v1/chat/completions', {'model': 'fixture-a', 'stream': True, 'messages': [{'role': 'system', 'content': 'x'}, {'role': 'user', 'content': 'weather?'}], 'tools': tools})
        objs = [json.loads(l[6:]) for l in lines[:-1]]; self.assertTrue(any('tool_calls' in o['choices'][0].get('delta', {}) for o in objs if o['choices']))
        s, _, b = self.h.req('POST', '/v1/completions', {'model': 'fixture-a', 'prompt': 'complete me'}); self.assertIn('complete me', b['choices'][0]['text'])
        s, _, b = self.h.req('POST', '/v1/chat/completions', {'model': 'fixture-a', 'n': 2, 'messages': [{'role': 'user', 'content': 'x'}]}); self.assertEqual(s, 400)
    def test_08_errors(self):
        self.assertEqual(self.h.req('POST', '/api/chat', {'model': 'nope', 'messages': [{'role': 'user', 'content': 'x'}]})[0], 404)
        self.assertEqual(self.h.req('POST', '/api/chat', '{bad json')[0], 400)
        self.assertEqual(self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'messages': [{'role': 'wizard', 'content': 'x'}]})[0], 400)
        self.assertEqual(self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {}}]}]})[0], 400)
        self.assertEqual(self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'options': {'temperature': 9}, 'messages': [{'role': 'user', 'content': 'x'}]})[0], 400)
        self.assertEqual(self.h.req('POST', '/api/embed', {'model': 'fixture-a', 'input': 'x'})[0], 501)
        self.assertEqual(self.h.req('POST', '/api/pull', {'model': 'x'})[0], 501)
        self.assertEqual(self.h.req('GET', '/nothing')[0], 404)
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'options': {'num_ctx': 128}, 'messages': [{'role': 'user', 'content': 'w ' * 300}]})
        self.assertEqual(s, 400); self.assertEqual(b['code'], 'context_overflow')
        s, _, raw = self.h.req('POST', '/api/chat', 'x' * 100, {'Content-Length': '100'}, raw=True); self.assertEqual(s, 400)
        import socket
        for raw, want in [(b'get / HTTP/1.1\r\n\r\n', b' 400 '), (b'GET api/version HTTP/1.1\r\n\r\n', b' 400 '), (b'GET / HTTP\r\n\r\n', b' 400 '), (b'GET / HTTP/1.1\r\nBadHeader\r\n\r\n', b' 400 '),
                          (b'GET / HTTP/1.1\r\nContent-Length: -5\r\n\r\n', b' 400 '), (b'POST /api/chat HTTP/1.1\r\nContent-Length: 7\r\n\r\n[1,2,3]', b' 400 '), (b'POST /api/chat HTTP/1.1\r\nContent-Length: 2\r\n\r\n{a', b' 400 ')]:
            sk = socket.create_connection(('127.0.0.1', self.h.port)); sk.settimeout(5); sk.sendall(raw); self.assertIn(want, sk.recv(200), raw); sk.close()
        c = http.client.HTTPConnection('127.0.0.1', self.h.port); c.putrequest('POST', '/api/chat'); c.putheader('Content-Length', '99999999999'); c.endheaders(); self.assertEqual(c.getresponse().status, 413)
        import socket
        sk = socket.create_connection(('127.0.0.1', self.h.port)); sk.sendall(b'POST /api/chat HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}x'); self.assertIn(b' 400 ', sk.recv(200)); sk.close()
        c = http.client.HTTPConnection('127.0.0.1', self.h.port); c.putrequest('POST', '/api/chat'); c.putheader('Transfer-Encoding', 'chunked'); c.endheaders(); self.assertEqual(c.getresponse().status, 501)
    def test_09_format_json(self):
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'format': 'json', 'messages': [{'role': 'user', 'content': 'give JSON'}]})
        self.assertEqual(json.loads(b['message']['content']), {'ok': True})
    def test_10_load_unload_ps(self):
        s, _, b = self.h.req('POST', '/native/load', {'model': 'fixture-b'}); self.assertEqual(b['status'], 'loaded')
        self.assertEqual(self.h.req('GET', '/api/ps')[2]['models'][0]['name'], 'fixture-b')
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': 'swap'}]}); self.assertEqual(s, 200)
        self.assertEqual(self.h.req('GET', '/api/ps')[2]['models'][0]['name'], 'fixture-a')
        s, _, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'keep_alive': 0, 'messages': []}); self.assertEqual(b['done_reason'], 'unload'); self.assertEqual(self.h.req('GET', '/api/ps')[2]['models'], [])
        s, _, b = self.h.req('POST', '/api/generate', {'model': 'fixture-b', 'prompt': ''}); self.assertEqual(b['done_reason'], 'load'); self.assertEqual(self.h.req('GET', '/api/ps')[2]['models'][0]['name'], 'fixture-b')
        self.assertEqual(self.h.req('POST', '/native/unload', {'release': True})[0], 409)
        self.assertEqual(self.h.req('POST', '/native/unload', {})[2]['status'], 'unloaded')
    def test_11_config(self):
        s, _, b = self.h.req('POST', '/native/config', {'default_num_ctx': 1024, 'default_think': True}); self.assertEqual(s, 200); self.assertEqual(b['config']['default_num_ctx'], 1024)
        s, h, b = self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': 'cfg'}]})
        self.assertEqual(h['X-Native-Context'], '1024'); self.assertIn('thinking', b['message'])
        for bad in (['10.0.0.0/'], ['10.0.0.0//8'], ['010.0.0.0/8'], ['10.0.0.0/08'], ['10.0.0.0/33'], [''], ['0x7f.0.0.1'], ['10.0.0.0/ 8']):   # an empty/garbled prefix must never be accepted (atoi('') == 0 would mean /0 = trust everyone)
            s_, _, b_ = self.h.req('POST', '/native/config', {'trusted_cidrs': bad}); self.assertEqual(s_, 400, bad)
        self.assertEqual(self.h.req('GET', '/native/config')[2]['trusted_cidrs'], ['127.0.0.0/8'])   # rejected values were not applied
        self.assertEqual(self.h.req('POST', '/native/config', {'default_num_ctx': -5})[0], 400); self.assertEqual(self.h.req('POST', '/native/config', {'bogus': 1})[0], 400)
        self.assertEqual(self.h.req('POST', '/native/config', {'default_num_ctx': 0, 'default_think': False})[0], 200)
        self.assertEqual(json.load(open(self.h.cfgpath))['default_think'], False)
        st = self.h.req('GET', '/native/status')[2]; self.assertIn('top_p', st['ignored_options_seen']); self.assertGreater(st['requests'], 10)
    def test_12_cancel_and_concurrency(self):
        self.h.req('POST', '/native/config', {'queue_wait_s': 30})
        c = http.client.HTTPConnection('127.0.0.1', self.h.port); c.request('POST', '/api/chat', json.dumps({'model': 'fixture-a', 'messages': [{'role': 'user', 'content': 'say long'}]}))
        r = c.getresponse(); r.fp.readline(); r.fp.readline(); r.close(); c.close()   # disconnect mid-stream
        time.sleep(0.5)
        res = []
        def go(i): res.append(self.h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': 'par %d' % i}]})[0])
        ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]; [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(res, [200] * 4)
        log = self.h.req('GET', '/native/log?n=100')[2]['log']; self.assertTrue(any('cancelled by client' in l for l in log), log[-5:])

    def test_13_anthropic(self):
        h = self.h
        s, _, b = h.req('POST', '/v1/messages', {'model': 'fixture-a', 'max_tokens': 100, 'system': [{'type': 'text', 'text': 'be brief'}], 'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hello claude'}]}]})
        self.assertEqual((s, b['type'], b['role'], b['stop_reason']), (200, 'message', 'assistant', 'end_turn')); self.assertIn('hello claude', b['content'][0]['text']); self.assertGreater(b['usage']['output_tokens'], 0)
        s, _, b = h.req('POST', '/v1/messages', {'model': 'fixture-a', 'max_tokens': 5, 'messages': [{'role': 'user', 'content': 'say long'}]}); self.assertEqual(b['stop_reason'], 'max_tokens')
        s, _, b = h.req('POST', '/v1/messages', {'model': 'fixture-a', 'max_tokens': 500, 'stop_sequences': ['word3 '], 'messages': [{'role': 'user', 'content': 'say long'}]}); self.assertEqual((b['stop_reason'], b['stop_sequence']), ('stop_sequence', 'word3 '))
        s, _, b = h.req('POST', '/v1/messages', {'model': 'fixture-a', 'max_tokens': 50, 'thinking': {'type': 'enabled', 'budget_tokens': 64}, 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual([c['type'] for c in b['content']], ['thinking', 'text'])
        tools = [{'name': 'get_weather', 'description': 'w', 'input_schema': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}]
        s, _, b = h.req('POST', '/v1/messages', {'model': 'fixture-a', 'max_tokens': 200, 'tools': tools, 'system': 's', 'messages': [{'role': 'user', 'content': 'weather please'}]})
        self.assertEqual(b['stop_reason'], 'tool_use'); tu = [c for c in b['content'] if c['type'] == 'tool_use'][0]; self.assertEqual(tu['input'], {'city': 'Paris'}); self.assertTrue(tu['id'].startswith('toolu_'))
        s, _, b2 = h.req('POST', '/v1/messages', {'model': 'fixture-a', 'max_tokens': 200, 'tools': tools, 'system': 's', 'messages': [{'role': 'user', 'content': 'weather please'}, {'role': 'assistant', 'content': b['content']},
                         {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tu['id'], 'content': [{'type': 'text', 'text': '21C'}]}]}]})
        self.assertEqual(s, 200); self.assertIn('The tool said', b2['content'][0]['text']); self.assertEqual(b2['stop_reason'], 'end_turn')
        s, _, b3 = h.req('POST', '/v1/messages', {'model': 'fixture-a', 'max_tokens': 50, 'system': 'top', 'messages': [{'role': 'user', 'content': 'first'}, {'role': 'system', 'content': [{'type': 'text', 'text': 'late reminder'}]}]}); self.assertEqual(s, 200)
        # streaming event grammar
        st, lines = h.stream('/v1/messages', {'model': 'fixture-a', 'max_tokens': 200, 'stream': True, 'tools': tools, 'system': 's', 'thinking': {'type': 'enabled', 'budget_tokens': 64}, 'messages': [{'role': 'user', 'content': 'weather please'}]})
        events = [l[7:] for l in lines if l.startswith('event: ')]; datas = [json.loads(l[6:]) for l in lines if l.startswith('data: ')]
        self.assertEqual(events[0], 'message_start'); self.assertEqual(events[-2:], ['message_delta', 'message_stop']); self.assertEqual(events.count('content_block_start'), events.count('content_block_stop'))
        starts = [d['content_block']['type'] for d in datas if d['type'] == 'content_block_start']; self.assertEqual(starts, ['thinking', 'text', 'tool_use'])
        self.assertEqual([d['index'] for d in datas if d['type'] == 'content_block_start'], [0, 1, 2])
        pj = ''.join(d['delta']['partial_json'] for d in datas if d['type'] == 'content_block_delta' and d['delta']['type'] == 'input_json_delta'); self.assertEqual(json.loads(pj), {'city': 'Paris'})
        self.assertEqual([d for d in datas if d['type'] == 'message_delta'][0]['delta']['stop_reason'], 'tool_use')
        self.assertNotIn('tool_call', ''.join(d['delta'].get('text', '') for d in datas if d['type'] == 'content_block_delta'))
        # errors use the Anthropic envelope
        s, _, e = h.req('POST', '/v1/messages', {'model': 'fixture-a', 'messages': [{'role': 'user', 'content': 'x'}]}); self.assertEqual((s, e['type'], e['error']['type']), (400, 'error', 'invalid_request_error'))
        s, _, e = h.req('POST', '/v1/messages', {'model': 'nope', 'max_tokens': 5, 'messages': [{'role': 'user', 'content': 'x'}]}); self.assertEqual((s, e['error']['type']), (404, 'not_found_error'))
        s, _, e = h.req('POST', '/v1/messages', {'model': 'fixture-a', 'max_tokens': 5, 'messages': [{'role': 'user', 'content': [{'type': 'image', 'source': {}}]}]}); self.assertEqual(s, 400)
        s, _, c = h.req('POST', '/v1/messages/count_tokens', {'model': 'fixture-a', 'messages': [{'role': 'user', 'content': 'one two three'}]}); self.assertEqual(s, 200); self.assertGreater(c['input_tokens'], 3)


    def test_14_queue_limits_and_fairness(self):
        h = self.h; self.assertEqual(h.req('POST', '/native/config', {'max_queue': 1, 'queue_wait_s': 30})[0], 200)
        try:
            codes = {}
            def go(name, content, delay):
                time.sleep(delay); codes[name] = h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': content}]})[0]
            ts = [threading.Thread(target=go, args=('A', 'say long', 0)), threading.Thread(target=go, args=('B', 'queued', 0.25)), threading.Thread(target=go, args=('C', 'overflow', 0.45))]
            [t.start() for t in ts]; [t.join() for t in ts]
            self.assertEqual(codes, {'A': 200, 'B': 200, 'C': 503})
            s, _, b = h.req('POST', '/api/chat', {'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': 'x'}]}); self.assertEqual(s, 200)
            # metadata endpoints stay responsive while the GPU is busy and the queue is full
            t = threading.Thread(target=go, args=('D', 'say long', 0)); t.start(); time.sleep(0.2)
            t0 = time.monotonic(); self.assertEqual(h.req('GET', '/api/tags')[0], 200); self.assertLess(time.monotonic() - t0, 0.5); t.join()
            # a queued client that disconnects never reaches the GPU
            before = h.req('GET', '/native/status')[2]['backend']
            t = threading.Thread(target=go, args=('E', 'say long', 0)); t.start(); time.sleep(0.2)
            c = http.client.HTTPConnection('127.0.0.1', h.port); c.request('POST', '/api/chat', json.dumps({'model': 'fixture-a', 'stream': False, 'messages': [{'role': 'user', 'content': 'abandoned'}]})); time.sleep(0.2); c.close()
            t.join(); time.sleep(0.6)
            log = h.req('GET', '/native/log?n=60')[2]['log']
            self.assertFalse(any('abandoned' in l for l in log)); self.assertTrue(any('client_gone' in l for l in log), log[-6:])
        finally:
            h.req('POST', '/native/config', {'max_queue': 8, 'queue_wait_s': 600})

class Auth(unittest.TestCase):
    def test_untrusted_needs_key(self):
        h = Host({'trusted_cidrs': ['10.9.9.0/24'], 'api_key': 'sekrit'})
        try:
            self.assertEqual(h.req('GET', '/api/tags')[0], 403)
            self.assertEqual(h.req('GET', '/api/tags', headers={'Authorization': 'Bearer wrong'})[0], 403)
            self.assertEqual(h.req('GET', '/api/tags', headers={'Authorization': 'Bearer sekrit'})[0], 200)
            self.assertEqual(h.req('GET', '/api/tags', headers={'X-API-Key': 'sekrit'})[0], 200)
            self.assertNotIn('sekrit', json.dumps(h.req('GET', '/native/config', headers={'X-API-Key': 'sekrit'})[2]))
            for hdr, want in [({'Authorization': 'bearer sekrit'}, 200), ({'Authorization': 'BEARER   sekrit'}, 200), ({'Authorization': 'Bearer sekrit '}, 200), ({'Authorization': 'Bearersekrit'}, 403), ({'Authorization': 'Bearer sekri'}, 403),
                              ({'Authorization': 'Bearer sekrett'}, 403), ({'Authorization': 'Basic sekrit'}, 403), ({'X-API-Key': 'sekrit\x00'.replace('\x00', 'x')}, 403), ({'X-API-Key': 'SEKRIT'}, 403), ({'Authorization': 'Bearer '}, 403)]:
                self.assertEqual(h.req('GET', '/api/tags', headers=hdr)[0], want, hdr)
        finally: h.stop()

if __name__ == '__main__':
    unittest.main(verbosity=2)
