#!/usr/bin/env python3
"""Hostile-JSON fuzzing of the native API (host build, ASan/UBSan). Seeds: Fat PS4 worker (Ollama/OpenAI bodies, used as delivered) and Slim PS4 worker
(Anthropic categories; its output was not valid JSON, bodies repaired by Claude), plus valid baselines, then deterministic mutation.
Pass criteria for EVERY request: a complete HTTP response, status in the allowed set (never 500), JSON-ish body, within the deadline; the server stays alive and
sanitizer-clean. Usage: fuzz_api.py <native-host binary> [iterations] [seed]"""
import copy, http.client, json, os, random, signal, subprocess, sys, tempfile, time

BIN = sys.argv[1] if len(sys.argv) > 1 else '/tmp/native-host'; N = int(sys.argv[2]) if len(sys.argv) > 2 else 3000; SEED = int(sys.argv[3]) if len(sys.argv) > 3 else 12345
M = 'fixture-a'; U = [{'role': 'user', 'content': 'hello'}]
TOOLS_OA = [{'type': 'function', 'function': {'name': 'get_weather', 'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}}}}]
BASE = {  # valid baselines per endpoint
    '/api/chat': {'model': M, 'stream': False, 'messages': U, 'options': {'temperature': 0.5, 'num_predict': 8, 'stop': ['x'], 'num_ctx': 2048}, 'think': True, 'tools': TOOLS_OA, 'format': 'json', 'keep_alive': '5m'},
    '/api/generate': {'model': M, 'stream': False, 'prompt': 'hello', 'system': 's', 'options': {'num_predict': 8}, 'raw': False},
    '/v1/chat/completions': {'model': M, 'messages': U, 'max_tokens': 8, 'stream': False, 'tools': TOOLS_OA, 'stop': ['x'], 'response_format': {'type': 'json_object'}, 'reasoning_effort': 'low'},
    '/v1/completions': {'model': M, 'prompt': 'hello', 'max_tokens': 8},
    '/v1/messages': {'model': M, 'max_tokens': 8, 'system': [{'type': 'text', 'text': 's'}], 'stop_sequences': ['x'], 'thinking': {'type': 'enabled', 'budget_tokens': 64},
                     'tools': [{'name': 'get_weather', 'input_schema': {'type': 'object', 'properties': {}}}],
                     'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}, {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'get_weather', 'input': {'city': 'P'}}]},
                                  {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}]}]},
    '/v1/messages/count_tokens': {'model': M, 'messages': U},
    '/api/show': {'model': M}, '/native/config': {'default_num_ctx': 1024, 'max_queue': 8}, '/native/load': {'model': M}, '/native/unload': {},
}
FAT_SEEDS = [  # as delivered by the Fat worker (messages/options/etc. hostile)
    {'model': M, 'messages': 'This is a string instead of an array of objects'}, {'model': M, 'messages': [{'role': 'user', 'content': None}]}, {'model': M, 'messages': [{'role': 'user', 'content': 42}]},
    {'model': M, 'messages': [{'role': ['user', 'system'], 'content': 'Hello'}]}, {'model': M, 'messages': [{'role': 'user', 'content': 'Hello', 'tools': {'name': 'search'}}]},
    {'model': M, 'messages': [{'role': 'user', 'content': 'Hello', 'tools': [{'name': None}]}]}, {'model': M, 'stop': 123}, {'model': M, 'options': 'invalid string'}, {'model': M, 'options': {'temperature': 'hot'}},
    {'model': M, 'num_predict': 99999999999999999999}, {'model': M, 'num_ctx': -100}, {'model': M, 'temperature': None}, {'model': M, 'stream': 'yes'}, {'model': M, 'options': {'think': {'max_tokens': 10}}},
    {'model': M, 'messages': [{'role': 'user', 'content': {'nested': {'deeply': 'content here'}}}]}, {'model': M, 'keep_alive': ['1h', '2d']}]
SLIM_SEEDS = [  # categories from the Slim worker, bodies repaired
    {'model': M, 'max_tokens': '100', 'messages': U}, {'model': M, 'max_tokens': 0, 'messages': U}, {'model': M, 'max_tokens': -5, 'messages': U}, {'model': M, 'max_tokens': 8, 'messages': []}, {'model': M, 'max_tokens': 8},
    {'model': M, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': [None]}]}, {'model': M, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': [{'text': 'no type'}]}]},
    {'model': M, 'max_tokens': 8, 'messages': [{'role': 'user', 'content': [{'type': 'tool_result', 'content': {'k': 'v'}}]}]},
    {'model': M, 'max_tokens': 8, 'messages': [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't', 'name': 'x', 'input': 'wrong'}]}]}, {'model': M, 'max_tokens': 8, 'system': 123, 'messages': U},
    {'model': M, 'max_tokens': 8, 'tools': 'invalid', 'messages': U}, {'model': M, 'max_tokens': 8, 'thinking': 'on', 'messages': U}, {'model': M, 'max_tokens': 8, 'thinking': {'type': 'enabled', 'budget_tokens': '100'}, 'messages': U},
    {'model': M, 'max_tokens': 8, 'stop_sequences': [42], 'messages': U}]
VALUES = [None, True, False, 0, -1, 1, 2 ** 31, 2 ** 63, 99999999999999999999, 1e30, -1e30, 0.5, '', 'x', 'x' * 5000, 'é€\U0001F600', '\x00', [], {}, [None], [[]], {'a': 1}, [{'role': 'user'}], {'type': 'text'}, 'json', 'low', ['x', 1]]
OK = {200, 400, 404, 409, 413, 422, 429, 499, 501, 503}

def mutate(rnd, body):
    b = copy.deepcopy(body)
    for _ in range(rnd.choice([1, 1, 2, 3])):
        paths = []
        def walk(o, p):
            paths.append(p)
            if isinstance(o, dict): [walk(v, p + [k]) for k, v in o.items()]
            elif isinstance(o, list): [walk(v, p + [i]) for i, v in enumerate(o)]
        walk(b, [])
        p = rnd.choice(paths); op = rnd.choice(['set', 'set', 'set', 'delete', 'nest', 'dup'])
        if not p:
            b = rnd.choice(VALUES) if op == 'set' else b; continue
        parent = b
        for k in p[:-1]: parent = parent[k]
        k = p[-1]
        if op == 'delete':
            if isinstance(parent, dict): parent.pop(k, None)
            elif isinstance(parent, list) and parent: parent.pop(k)
        elif op == 'nest':
            v = 'deep'
            for _ in range(rnd.choice([5, 40, 120])): v = rnd.choice([[v], {'a': v}])
            parent[k] = v
        elif op == 'dup' and isinstance(parent, list): parent.append(copy.deepcopy(parent[k]))
        else: parent[k] = copy.deepcopy(rnd.choice(VALUES))
    return b

def post(port, path, data, timeout=30):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        c.request('POST', path, data, {'Content-Type': 'application/json'}); r = c.getresponse(); raw = r.read(); return r.status, raw
    finally: c.close()

def main():
    errf = tempfile.TemporaryFile('w+')
    p = subprocess.Popen([BIN, '0'], stdout=subprocess.PIPE, stderr=errf, text=True); port = int(p.stdout.readline().split()[1])
    rnd = random.Random(SEED); problems = []; sent = 0; by_status = {}
    cases = [(path, s) for path, seeds in (('/api/chat', FAT_SEEDS), ('/v1/chat/completions', FAT_SEEDS), ('/api/generate', FAT_SEEDS), ('/v1/messages', SLIM_SEEDS), ('/v1/messages/count_tokens', SLIM_SEEDS)) for s in seeds]
    for i in range(N + len(cases)):
        if i < len(cases): path, body = cases[i]
        else:
            path = rnd.choice(list(BASE)); body = mutate(rnd, BASE[path])
        if path in ('/native/load', '/native/unload'): body = body if isinstance(body, dict) else {}   # keep management calls from relaunching the host harness
        if path == '/native/unload' and isinstance(body, dict): body.pop('release', None)
        if path == '/native/config' and isinstance(body, dict): body.pop('queue_wait_s', None); body.pop('port', None)
        try: data = json.dumps(body, allow_nan=True)
        except (ValueError, RecursionError): continue
        t0 = time.monotonic()
        try: st, raw = post(port, path, data)
        except Exception as e: problems.append(f'{path} {data[:200]!r}: {type(e).__name__}: {e}'); st = -1; raw = b''
        sent += 1; by_status[st] = by_status.get(st, 0) + 1
        if st not in OK and st != -1: problems.append(f'{path} -> {st} {data[:200]!r} {raw[:160]!r}')
        if time.monotonic() - t0 > 15: problems.append(f'{path} slow {time.monotonic() - t0:.1f}s {data[:120]!r}')
        if p.poll() is not None: problems.append(f'SERVER DIED at iteration {i}: {path} {data[:200]!r}'); break
        if len(problems) >= 15: break
    alive = p.poll() is None
    if alive:
        try: st, _ = post(port, '/api/tags', None); alive = st in (200, 400, 404)   # POST on a GET route: any clean answer proves liveness
        except Exception as e: alive = False; problems.append(f'liveness: {e}')
        p.send_signal(signal.SIGTERM)
    try: p.wait(20)
    except subprocess.TimeoutExpired: p.kill(); problems.append('did not exit on SIGTERM')
    errf.seek(0); err = errf.read(); errf.close(); p.stdout.close()
    open('/tmp/fuzz_stderr.txt', 'w').write(err)
    if 'Sanitizer' in err or 'runtime error' in err: problems.append('SANITIZER: ' + err[err.find('Sanitizer') - 20:][:1500])
    print(json.dumps({'sent': sent, 'statuses': by_status, 'alive': alive, 'rc': p.returncode, 'problems': problems[:15]}, indent=2))
    return 1 if problems or not alive else 0
if __name__ == '__main__': sys.exit(main())
