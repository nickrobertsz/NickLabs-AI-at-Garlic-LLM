"""Hub tests against fake consoles (real aiohttp servers that speak the app's API): registry, routing, agent scheduling, proxy, security, jobs, logs."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ['NLAG_CAPTURE'] = '0'
os.environ['NLAG_DISCOVERY'] = '0'   # tests must never scan the real network or register real consoles
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hub'))

from aiohttp import ClientSession, web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from nlag_hub import core, protocol, server, support  # noqa: E402
from nlag_hub.fleet import ConsoleGate  # noqa: E402


class FakeConsole:
    """What the hub sees of a console: /native/status, /api/tags, /native/load, /native/unload, streamed chat."""

    def __init__(self, models=('qwen3.5-4b-garlic', 'qwen3.5-9b-garlic'), loaded='', speak=0.25, swap_delay=0.0):
        self.models, self.loaded, self.speak, self.swap_delay = list(models), loaded, speak, swap_delay
        self.log = []            # (agent, model, t_start, t_end)
        self.active = 0
        self.max_active = 0
        self.config = {}
        self.app = web.Application()
        r = self.app.router
        r.add_get('/native/status', self.status)
        r.add_get('/api/tags', self.tags)
        r.add_post('/native/load', self.load)
        r.add_post('/native/unload', self.unload)
        r.add_post('/native/config', self.set_config)
        r.add_post('/v1/chat/completions', self.chat)
        r.add_post('/api/chat', self.chat)
        self.server = None

    async def start(self):
        self.server = TestServer(self.app)
        await self.server.start_server()
        return self

    @property
    def port(self):
        return self.server.port

    async def status(self, request):
        return web.json_response({'app': 'nicklabs-native', 'build': 'fake-1', 'loaded': self.loaded, 'uptime_s': 5, 'requests': len(self.log), 'errors': 0, 'ctx_limit': 16384,
                                  'backend': {'tokens_per_second': 33.0, 'gpu_reserved_bytes': 4664066048 if self.loaded else None}, 'config': dict(self.config), 'agents': {}})

    async def tags(self, request):
        return web.json_response({'models': [{'name': m, 'model': m, 'size': 2_000_000_000} for m in self.models]})

    async def set_config(self, request):
        self.config.update(await request.json())
        return web.json_response({'ok': True})

    async def load(self, request):
        m = (await request.json())['model']
        if m not in self.models:
            return web.json_response({'error': 'no such model'}, status=404)
        if self.loaded == m:
            return web.json_response({'status': 'loaded'})
        if self.loaded and self.swap_delay:       # a real console relaunches into the new model
            self.loaded = ''
            asyncio.get_running_loop().call_later(self.swap_delay, lambda: setattr(self, 'loaded', m))
            return web.json_response({'code': 'swapping', 'error': 'restarting'}, status=503)
        self.loaded = m
        return web.json_response({'status': 'loaded'})

    async def unload(self, request):
        self.loaded = ''
        return web.json_response({'status': 'restarting'}, status=202)

    async def chat(self, request):
        body = await request.json()
        agent = request.headers.get('X-Agent-Id', '')
        if body['model'] != self.loaded:
            return web.json_response({'error': 'not loaded'}, status=503)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        t0 = time.monotonic()
        resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await resp.prepare(request)
        await resp.write(b'data: ' + json.dumps({'model': body['model'], 'choices': [{'delta': {'role': 'assistant'}}]}).encode() + b'\n\n')
        await asyncio.sleep(self.speak)
        await resp.write(b'data: ' + json.dumps({'model': body['model'], 'choices': [{'delta': {'content': f'hi from {agent}'}}]}).encode() + b'\n\n')
        await resp.write(b'data: [DONE]\n\n')
        await resp.write_eof()
        self.active -= 1
        self.log.append((agent, body['model'], t0, time.monotonic()))
        return resp


class HubCase(unittest.IsolatedAsyncioTestCase):
    consoles = 2

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fakes = [await FakeConsole().start() for _ in range(self.consoles)]
        self.app = server.create_app(self.tmp.name)
        self.srv = TestServer(self.app)
        await self.srv.start_server()
        self.http = ClientSession()
        self.url = str(self.srv.make_url('')).rstrip('/')
        for i, f in enumerate(self.fakes):
            await self.api('POST', '/hub/v1/consoles', {'host': '127.0.0.1', 'api_port': f.port, 'name': f'Console {i + 1}'})
        await self.settle()

    async def asyncTearDown(self):
        await self.http.close()
        await self.srv.close()
        for f in self.fakes:
            await f.server.close()
        self.tmp.cleanup()

    async def api(self, method, path, body=None, headers=None, status=None):
        async with self.http.request(method, self.url + path, json=body, headers=headers) as r:
            data = await r.json(content_type=None) if r.content_type == 'application/json' else await r.read()
            if status is not None:
                self.assertEqual(r.status, status, data)
            return data

    async def settle(self, n=1.5):
        fleet = self.app[server.FLEET]
        for node in fleet.nodes.values():
            await fleet.poll(node)

    async def chat(self, model, agent='a', via='/v1/chat/completions'):
        async with self.http.post(self.url + via, json={'model': model, 'messages': [{'role': 'user', 'content': 'x'}], 'stream': True}, headers={'X-Agent-Id': agent}) as r:
            return r.status, r.headers.get('X-NickLabs-Console'), await r.read()


class Registry(HubCase):
    async def test_ids_validation_and_n_consoles(self):
        st = await self.api('GET', '/hub/v1/state')
        self.assertEqual([c['id'] for c in st['consoles']], ['ps4-1', 'ps4-2'])
        for bad in ({'host': ''}, {'host': 'bad host!'}, {'host': '1.2.3.4', 'id': 'Bad Id'}, {'host': '1.2.3.4', 'api_port': 0}, {'host': '1.2.3.4', 'ftp_port': 'x'}):
            await self.api('POST', '/hub/v1/consoles', bad, status=400)
        await self.api('POST', '/hub/v1/consoles', {'host': '127.0.0.1', 'api_port': self.fakes[0].port}, status=400)   # duplicate address
        for i in range(3, 8):    # no limit of two (or three): seven consoles
            await self.api('POST', '/hub/v1/consoles', {'host': f'10.0.0.{i}', 'name': f'extra {i}'}, status=201)
        st = await self.api('GET', '/hub/v1/state')
        self.assertEqual(len(st['consoles']), 7)
        self.assertEqual([c['online'] for c in st['consoles']][:2], [True, True])
        await self.api('DELETE', '/hub/v1/consoles/ps4-3', status=200)
        self.assertEqual(len((await self.api('GET', '/hub/v1/state'))['consoles']), 6)
        await self.api('DELETE', '/hub/v1/consoles/nope', status=400)
        # persisted: a new Registry reads the same file
        self.assertEqual(len(core.Registry(Path(self.tmp.name) / 'consoles.json').list()), 6)


class Routing(HubCase):
    consoles = 3

    async def test_explicit_console_loads_and_routes(self):
        self.fakes[1].loaded = 'qwen3.5-4b-garlic'
        await self.settle()
        s, c, body = await self.chat('qwen3.5-4b-garlic')
        self.assertEqual((s, c), (200, 'ps4-2')); self.assertIn(b'hi from a', body)
        # explicit console that has not loaded it: the hub loads it (console is idle), then answers
        s, c, _ = await self.chat('qwen3.5-9b-garlic@ps4-3')
        self.assertEqual((s, c), (200, 'ps4-3')); self.assertEqual(self.fakes[2].loaded, 'qwen3.5-9b-garlic')
        # unknown model / console
        s, c, body = await self.chat('nope')
        self.assertEqual(s, 404); self.assertIn(b'not installed', body)
        s, _, body = await self.chat('qwen3.5-4b-garlic@ps4-9')
        self.assertEqual(s, 404)

    async def test_least_busy_and_sticky(self):
        for f in self.fakes[:2]:
            f.loaded = 'qwen3.5-4b-garlic'
            f.speak = 0.4
        await self.settle()
        res = await asyncio.gather(*[self.chat('qwen3.5-4b-garlic', f'agent{i}') for i in range(4)])
        used = sorted(c for _, c, _ in res)
        self.assertEqual(used, ['ps4-1', 'ps4-1', 'ps4-2', 'ps4-2'])      # new agents spread over the consoles serving the model
        for f in self.fakes[:2]:
            self.assertEqual(f.max_active, 1)                            # never two at once on one console
        first = {a: c for a, c in zip(['agent0', 'agent1', 'agent2', 'agent3'], [c for _, c, _ in res])}
        for a in ('agent0', 'agent3'):                                    # sticky: the same agent returns to the same console
            self.assertEqual((await self.chat('qwen3.5-4b-garlic', a))[1], first[a])

    async def test_auto_load_only_on_idle_console(self):
        self.fakes[0].loaded = 'qwen3.5-9b-garlic'; self.fakes[1].loaded = 'qwen3.5-9b-garlic'; self.fakes[2].loaded = 'qwen3.5-9b-garlic'
        await self.settle()
        s, _, body = await self.chat('qwen3.5-4b-garlic')                 # every console is busy with another model: do not steal one
        self.assertEqual(s, 409); self.assertIn(b'not loaded', body); self.assertIn(b'@ps4-1', body)
        await self.api('POST', '/hub/v1/consoles/ps4-3/unload', status=202)
        self.fakes[2].loaded = ''; await asyncio.sleep(1.7); await self.settle()    # (a console that was just told to unload is not "idle" for 1.5 s)
        s, c, _ = await self.chat('qwen3.5-4b-garlic')                    # now there is an idle console: it loads the model there
        self.assertEqual((s, c), (200, 'ps4-3'))

    async def test_swap_with_relaunch_is_waited_for(self):
        f = self.fakes[0]; f.loaded = 'qwen3.5-4b-garlic'; f.swap_delay = 1.2
        await self.settle()
        t = time.monotonic()
        s, c, _ = await self.chat('qwen3.5-9b-garlic@ps4-1')
        self.assertEqual((s, c), (200, 'ps4-1')); self.assertGreater(time.monotonic() - t, 1.0); self.assertEqual(f.loaded, 'qwen3.5-9b-garlic')

    async def test_model_list_merge_and_ps(self):
        self.fakes[0].loaded = 'qwen3.5-4b-garlic'; await self.settle()
        tags = await self.api('GET', '/api/tags')
        names = [m['name'] for m in tags['models']]
        self.assertEqual(sorted(names), ['qwen3.5-4b-garlic', 'qwen3.5-9b-garlic'])         # one entry per model, however many consoles have it
        self.assertEqual(tags['models'][0]['nicklabs']['consoles'], ['ps4-1', 'ps4-2', 'ps4-3'])
        allm = await self.api('GET', '/api/tags?all=1')
        self.assertIn('qwen3.5-4b-garlic@ps4-2', [m['name'] for m in allm['models']])
        self.assertEqual([m['id'] for m in (await self.api('GET', '/v1/models'))['data']], names)
        self.assertEqual((await self.api('GET', '/api/ps'))['models'][0]['nicklabs']['console'], 'ps4-1')

    async def test_offline_console_is_skipped(self):
        for f in self.fakes:
            f.loaded = 'qwen3.5-4b-garlic'
        await self.settle()
        await self.fakes[0].server.close(); await self.settle()
        for a in ('x', 'y', 'z'):
            s, c, _ = await self.chat('qwen3.5-4b-garlic', a)
            self.assertEqual(s, 200); self.assertNotEqual(c, 'ps4-1')
        self.assertFalse((await self.api('GET', '/hub/v1/state'))['consoles'][0]['online'])


class Agents(HubCase):
    consoles = 2

    async def test_round_robin_one_speaker_per_console_parallel_across(self):
        a, b = self.fakes
        a.loaded = 'qwen3.5-4b-garlic'; b.loaded = 'qwen3.5-9b-garlic'; a.speak = 0.6; b.speak = 0.6
        await self.settle()
        tasks = [asyncio.create_task(self.chat('qwen3.5-4b-garlic', 'alice')), ]
        await asyncio.sleep(0.1)
        for ag in ('alice', 'alice', 'bob', 'carol'):
            tasks.append(asyncio.create_task(self.chat('qwen3.5-4b-garlic', ag)))
            await asyncio.sleep(0.03)
        tasks.append(asyncio.create_task(self.chat('qwen3.5-9b-garlic', 'dave')))      # the other console, at the same time
        await asyncio.sleep(0.15)
        st = await self.api('GET', '/hub/v1/state')
        c1, c2 = st['consoles']
        self.assertEqual(c1['agents']['speaking'], 'alice'); self.assertEqual(c1['agents']['queued'], {'alice': 2, 'bob': 1, 'carol': 1})
        self.assertEqual(c2['agents']['speaking'], 'dave')
        await asyncio.gather(*tasks)
        order = [x[0] for x in a.log]
        self.assertEqual(order[0], 'alice'); self.assertEqual(sorted(order[1:3]), ['bob', 'carol']); self.assertEqual(order[3:], ['alice', 'alice'])
        self.assertEqual(a.max_active, 1)
        self.assertLess(b.log[0][2], a.log[0][3])          # dave started while alice was still speaking: consoles run in parallel
        agents = (await self.api('GET', '/hub/v1/state'))['agents']['seen']
        self.assertEqual(agents['alice']['requests'], 3)

    async def test_agent_from_openai_user_and_metadata(self):
        self.fakes[0].loaded = 'qwen3.5-4b-garlic'; await self.settle()
        async with self.http.post(self.url + '/v1/chat/completions', json={'model': 'qwen3.5-4b-garlic', 'user': 'story-teller', 'messages': []}) as r:
            await r.read()
        async with self.http.post(self.url + '/v1/messages', json={'model': 'qwen3.5-4b-garlic', 'metadata': {'user_id': 'narrator'}, 'messages': []}) as r:
            await r.read()
        self.assertEqual([x[0] for x in self.fakes[0].log][:1], ['story-teller'])
        self.assertIn('narrator', (await self.api('GET', '/hub/v1/state'))['agents']['seen'])

    async def test_gate_cancel_while_waiting(self):
        g = ConsoleGate()
        await g.acquire('a')
        t = asyncio.create_task(g.acquire('b'))
        await asyncio.sleep(0.05)
        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t
        self.assertEqual(g.snapshot()['queued'], {})
        g.release()
        self.assertFalse(g.busy)


class Streaming(HubCase):
    consoles = 1

    async def test_model_name_is_rewritten_and_ttft_measured(self):
        self.fakes[0].loaded = 'qwen3.5-4b-garlic'; await self.settle()
        s, c, body = await self.chat('qwen3.5-4b-garlic@ps4-1')
        self.assertEqual(s, 200)
        self.assertIn(b'"model": "qwen3.5-4b-garlic@ps4-1"', body.replace(b'"model":"', b'"model": "'))
        m = (await self.api('GET', '/hub/v1/state'))['metrics']
        self.assertEqual((m['ok'], m['failed']), (1, 0)); self.assertGreaterEqual(m['ttft_ms_median'], 200)   # first TEXT chunk, not the role header
        s, _, _ = await self.chat('nope'); self.assertEqual(s, 404)


class Security(HubCase):
    consoles = 1

    async def test_trusted_network_needs_no_key_and_untrusted_is_refused(self):
        await self.api('GET', '/hub/v1/state', status=200)                                  # default: no key on the trusted networks
        await self.api('POST', '/v1/chat/completions', {'model': 'x', 'messages': []}, headers={'Authorization': 'Bearer anything'}, status=404)   # any text works as a "key"
        await self.api('PUT', '/hub/v1/settings', {'trusted_networks': ['10.0.0.0/8']}, status=200)
        await self.api('GET', '/hub/v1/state', status=403)                                  # 127.0.0.1 is no longer trusted
        await self.api('GET', '/healthz', status=200)
        async with self.http.get(self.url + '/') as r:                                      # the dashboard page itself is public (it only asks for the key)
            self.assertEqual(r.status, 200)

    async def test_tool_keys_and_agent_binding(self):
        r = await self.api('POST', '/hub/v1/keys', {'name': 'claude-cli', 'agent': 'story teller!'}, status=201)
        key = r['key']
        self.assertTrue(key.startswith('nlag-')); self.assertEqual(r['record']['agent'], 'storyteller')
        listed = await self.api('GET', '/hub/v1/keys', status=200)
        self.assertEqual([k['name'] for k in listed['keys']], ['claude-cli']); self.assertNotIn(key, json.dumps(listed)); self.assertNotIn('hash', json.dumps(listed))
        self.fakes[0].loaded = 'qwen3.5-4b-garlic'; await self.settle()
        h = {'Authorization': 'Bearer ' + key, 'X-Agent-Id': 'impostor'}
        async with self.http.post(self.url + '/v1/chat/completions', json={'model': 'qwen3.5-4b-garlic', 'messages': []}, headers=h) as resp:
            await resp.read()
        self.assertEqual(self.fakes[0].log[-1][0], 'storyteller')                           # the key decides who speaks, not the header
        # from an untrusted network a key still works for AI requests, but never for management
        await self.api('PUT', '/hub/v1/settings', {'trusted_networks': ['10.0.0.0/8']}, status=200)
        async with self.http.post(self.url + '/v1/chat/completions', json={'model': 'qwen3.5-4b-garlic', 'messages': []}, headers={'Authorization': 'Bearer ' + key}) as resp:
            self.assertEqual(resp.status, 200); await resp.read()
        await self.api('GET', '/hub/v1/state', headers={'Authorization': 'Bearer ' + key}, status=403)
        async with self.http.post(self.url + '/v1/chat/completions', json={'model': 'qwen3.5-4b-garlic', 'messages': []}, headers={'Authorization': 'Bearer nlag-wrong'}) as resp:
            self.assertEqual(resp.status, 403)
        self.app[server.SETTINGS].values['trusted_networks'] = ['127.0.0.0/8']; self.app[server.SETTINGS].networks = self.app[server.SETTINGS]._nets()
        await self.api('DELETE', '/hub/v1/keys/' + r['record']['id'], status=200)
        self.assertEqual((await self.api('GET', '/hub/v1/keys'))['keys'], [])
        await self.api('DELETE', '/hub/v1/keys/nope', status=400)

    async def test_require_keys_needs_an_admin_key(self):
        await self.api('PUT', '/hub/v1/settings', {'require_keys': True}, status=400)       # would lock the user out of their own dashboard
        await self.api('PUT', '/hub/v1/settings', {'api_key': 'admin-' + 'k' * 20}, status=200)
        await self.api('PUT', '/hub/v1/settings', {'require_keys': True}, status=200)
        await self.api('GET', '/hub/v1/state', status=401)
        await self.api('POST', '/v1/chat/completions', {'model': 'x'}, status=401)
        adm = {'Authorization': 'Bearer admin-' + 'k' * 20}
        await self.api('GET', '/hub/v1/state', headers=adm, status=200)
        await self.api('PUT', '/hub/v1/settings', {'require_keys': False}, headers=adm, status=200)
        await self.api('GET', '/hub/v1/state', status=200)

    async def test_settings_validation(self):
        await self.api('PUT', '/hub/v1/settings', {'agent_routing': 'weird'}, status=400)
        await self.api('PUT', '/hub/v1/settings', {'trusted_networks': ['not a net']}, status=400)
        await self.api('PUT', '/hub/v1/settings', {'bogus': 1}, status=400)
        s = await self.api('PUT', '/hub/v1/settings', {'agent_routing': 'spread', 'log_retention_days': 7}, status=200)
        self.assertEqual((s['agent_routing'], s['log_retention_days']), ('spread', 7)); self.assertNotIn('api_key', s)


class Discovery(HubCase):
    consoles = 0

    async def test_finds_and_registers_consoles_on_the_network(self):
        from nlag_hub.discovery import scan
        a, b = await FakeConsole().start(), await FakeConsole().start()
        try:
            import ipaddress
            fleet = self.app[server.FLEET]
            found = await scan([ipaddress.ip_network('127.0.0.0/30')], fleet.session, api_port=a.port)
            self.assertEqual([x['host'] for x in found['apps']], ['127.0.0.1']); self.assertEqual(found['apps'][0]['port'], a.port)
            self.assertEqual(found['candidates'], [])
            d = self.app[server.DISCOVERY]
            d.api_port = a.port
            os.environ['NLAG_SCAN_NETS'] = '127.0.0.0/30'
            res = await d.run()
            self.assertEqual(res['added'], ['ps4-1'])
            self.assertEqual([c['host'] for c in self.app[server.REGISTRY].list()], ['127.0.0.1'])
            res = await d.run()
            self.assertEqual(res['added'], [])                                  # already registered: not twice
            await self.api('DELETE', '/hub/v1/consoles/ps4-1', status=200)       # the user removed it ...
            res = await d.run()
            self.assertEqual(res['added'], [])                                  # ... so discovery leaves it alone
            res = await self.api('POST', '/hub/v1/discover', {'forget_removed': True}, status=200)
            self.assertEqual(res['added'], ['ps4-1'])                           # unless the user asks to forget that
        finally:
            os.environ.pop('NLAG_SCAN_NETS', None)
            await a.server.close(); await b.server.close()


class Logs(HubCase):
    consoles = 1

    async def test_support_bundle_redacts(self):
        root = core.DATA / 'logs' / 'ps4-1'
        root.mkdir(parents=True, exist_ok=True)
        (root / 'events-20260101.log').write_text('2026 [x] connect 192.168.1.50 from 10.1.2.3 mac aa:bb:cc:dd:ee:ff Authorization: Bearer abcdef123456789 RED(userId = deadbeef) /home/someone/x ::1 127.0.0.1 127.0.0.1\n')
        (root / 'events-20260101.log').touch()
        os.utime(root / 'events-20260101.log', None)
        z = await self.api('GET', '/hub/v1/support-bundle')
        import io, zipfile
        zf = zipfile.ZipFile(io.BytesIO(z))
        text = zf.read('logs/ps4-1/events-20260101.log').decode()
        for secret in ('192.168.1.50', '10.1.2.3', 'aa:bb:cc:dd:ee:ff', 'abcdef123456789', 'deadbeef', 'someone', 'RED'):
            self.assertNotIn(secret, text)
        self.assertEqual(support.Redactor()('x 127.0.0.1 0.0.0.0'), 'x 127.0.0.1 0.0.0.0')   # loopback / unspecified addresses are not private data
        self.assertEqual(text.count('<ip-1>') + text.count('<ip-2>') >= 2, True)
        self.assertNotIn('127.0.0.1', zf.read('consoles.json').decode().replace('"host": "<ip', ''))   # the console address itself is hidden too
        raw = zipfile.ZipFile(io.BytesIO(await self.api('GET', '/hub/v1/support-bundle?redact=0'))).read('logs/ps4-1/events-20260101.log').decode()
        self.assertIn('192.168.1.50', raw)
        self.assertEqual((await self.api('GET', '/hub/v1/logs/tail?path=ps4-1/events-20260101.log&n=5'))['lines'][0][:4], '2026')
        await self.api('GET', '/hub/v1/logs/tail?path=../settings.json', status=404)      # no escaping the log folder

    async def test_prune(self):
        from nlag_hub.capture import Capture
        cap = Capture(self.app[server.REGISTRY], self.app[server.SETTINGS], core.DATA / 'logs')
        d = core.DATA / 'logs' / 'ps4-1'; d.mkdir(parents=True, exist_ok=True)
        old, new = d / 'klog-20200101.log', d / 'klog-20260101.log'
        old.write_text('x'); new.write_text('y')
        os.utime(old, (time.time() - 90 * 86400,) * 2)
        cap.prune()
        self.assertFalse(old.exists()); self.assertTrue(new.exists())


JSDOM = os.getenv('JSDOM_FROM') or '/tmp/jsd/'


@unittest.skipUnless(__import__('shutil').which('node') and Path(JSDOM, 'node_modules/jsdom').exists(), 'node/jsdom unavailable')
class Dashboard(HubCase):
    consoles = 3

    async def test_dashboard_in_a_browser_engine(self):
        env = {**os.environ, 'JSDOM_FROM': JSDOM, 'HUB_URL': self.url}
        p = await asyncio.create_subprocess_exec('node', str(Path(__file__).parent / 'ui/hub.test.mjs'), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out = (await p.communicate())[0].decode()
        print(out)
        self.assertEqual(p.returncode, 0, out)


class Pure(unittest.TestCase):
    def test_split_model_and_agent(self):
        self.assertEqual(protocol.split_model('qwen3.5-4b-garlic@ps4-2'), ('qwen3.5-4b-garlic', 'ps4-2'))
        self.assertEqual(protocol.split_model('qwen3.5:4b'), ('qwen3.5:4b', None))
        self.assertEqual(protocol.split_model('x@Bad Id'), ('x@Bad Id', None))
        self.assertEqual(protocol.agent_of({}, {'user': 'a b/c<d>'}), 'abcd')
        self.assertEqual(protocol.agent_of({'X-Agent-Id': 'z' * 100}, {}), 'z' * 64)

    def test_registry_host_validation(self):
        for ok in ('192.168.1.5', 'ps4.local', 'my-ps4', '::1'):
            self.assertTrue(core.valid_host(ok), ok)
        for bad in ('', 'a b', 'http://x', 'x/y', '-x', 'a' * 300, None):
            self.assertFalse(core.valid_host(bad), bad)

    def test_model_shape_and_size_limits(self):
        from nlag_hub.convert import qwen35
        base = {'hidden_size': 2560, 'intermediate_size': 9216, 'num_hidden_layers': 32, 'num_attention_heads': 16, 'num_key_value_heads': 4, 'head_dim': 256, 'vocab_size': 248320,
                'linear_key_head_dim': 128, 'linear_num_key_heads': 16, 'linear_value_head_dim': 128, 'linear_num_value_heads': 32, 'layer_types': ['linear_attention'] * 3 + ['full_attention']}
        self.assertEqual(qwen35.shape_problem(base), '')                                   # the 4B shape
        self.assertTrue(qwen35.fits(base)[0])
        small = dict(base, hidden_size=2048, intermediate_size=6144, linear_num_value_heads=16)   # 0.8B/2B: linear input has 8224 rows
        self.assertIn('8224 rows', qwen35.shape_problem(small)); self.assertFalse(qwen35.fits(small)[0])
        big = dict(base, hidden_size=5120, intermediate_size=17408, num_hidden_layers=64, layer_types=(['linear_attention'] * 3 + ['full_attention']) * 16)
        self.assertFalse(qwen35.fits(big)[0]); self.assertGreater(qwen35.estimate_bytes(big), 10e9)

    def test_ollama_ref(self):
        from nlag_hub import sourcing
        self.assertEqual(sourcing.parse_ollama_ref('qwen3.5:4b'), ('library/qwen3.5', '4b'))
        self.assertEqual(sourcing.parse_ollama_ref('user/model'), ('user/model', 'latest'))
        for bad in ('../x', 'a b', 'x:y z', '', 'UPPER:1'):
            with self.assertRaises(sourcing.SourceError):
                sourcing.parse_ollama_ref(bad)
        with self.assertRaises(sourcing.SourceError):
            sourcing.pull_hf('not a repo', '/tmp/x')


if __name__ == '__main__':
    unittest.main()
