"""The fleet: live state of every registered console, the per-console agent scheduler, model routing and the streaming proxy.

Rules (the point of the hub):
* Consoles run in parallel; ONE request at a time per console (a console's GPU speaks for one agent at a time).
* Requests waiting for a console are admitted round-robin across agents (the agent served longest ago goes first; FIFO within an agent), so a chatty
  agent cannot starve the others.
* Several agents may share a model: they simply queue on the console that has it. With several consoles serving the same model, an agent is pinned
  (sticky) to one console so that console's prompt cache stays warm, and new agents go to the least busy one.
* No console is special: everything is derived from the registry."""
import asyncio
import collections
import json
import statistics
import time

import aiohttp
from aiohttp import web

from . import protocol


class NoRoute(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class ConsoleGate:
    """Async admission gate: concurrency 1, round-robin across agents."""

    def __init__(self):
        self.busy = False
        self.current = ''
        self.waiters = []           # [seq, agent, future]
        self.seq = 0
        self.served = 0
        self.last_served = {}       # agent -> admission counter
        self.since = 0.0

    def depth(self):
        return len(self.waiters) + (1 if self.busy else 0)

    async def acquire(self, agent):
        if not self.busy and not self.waiters:
            self._admit(agent)
            return
        fut = asyncio.get_running_loop().create_future()
        self.seq += 1
        entry = [self.seq, agent, fut]
        self.waiters.append(entry)
        try:
            await fut
        except BaseException:
            if entry in self.waiters:
                self.waiters.remove(entry)
            elif fut.done() and not fut.cancelled():
                self.release()   # we were admitted at the very moment we were cancelled: pass the turn on
            raise

    def _admit(self, agent):
        self.busy = True
        self.current = agent
        self.served += 1
        self.last_served[agent] = self.served
        self.since = time.time()
        if len(self.last_served) > 512:
            self.last_served.pop(next(iter(self.last_served)))

    def release(self):
        self.busy = False
        self.current = ''
        if not self.waiters:
            return
        best = min(self.waiters, key=lambda w: (self.last_served.get(w[1], 0), w[0]))
        self.waiters.remove(best)
        self._admit(best[1])
        if not best[2].done():
            best[2].set_result(True)
        else:
            self.release()

    def snapshot(self):
        queued = collections.Counter(w[1] or 'anonymous' for w in self.waiters)
        return {'speaking': (self.current or 'anonymous') if self.busy else '', 'queued': dict(queued), 'speaking_for_s': round(time.time() - self.since, 1) if self.busy else 0}


class Node:
    def __init__(self, cfg):
        self.cfg = cfg
        self.online = False
        self.status = None
        self.tags = []
        self.tags_at = 0.0
        self.error = ''
        self.last_seen = 0.0
        self.gate = ConsoleGate()
        self.pending = None   # {"action": "load"|"unload", "model": ..., "since": ...}
        self.used = 0.0       # last time a request was routed here (tie-break)

    @property
    def id(self):
        return self.cfg['id']

    @property
    def url(self):
        return f"http://{self.cfg['host']}:{self.cfg['api_port']}"

    @property
    def loaded(self):
        return (self.status or {}).get('loaded') or ''

    def installed(self):
        return [m['name'] for m in self.tags if isinstance(m, dict) and isinstance(m.get('name'), str)]

    def summary(self):
        st = self.status or {}
        be = st.get('backend') if isinstance(st.get('backend'), dict) else {}
        return {'id': self.id, 'name': self.cfg['name'], 'host': self.cfg['host'], 'online': self.online, 'loaded': self.loaded if self.online else '',
                'models': [{'name': m['name'], 'size': m.get('size'), 'loaded': self.online and m['name'] == self.loaded} for m in self.tags if isinstance(m, dict) and 'name' in m],
                'uptime_s': st.get('uptime_s'), 'build': st.get('build'), 'ctx_limit': st.get('ctx_limit'), 'gpu_reserved_bytes': be.get('gpu_reserved_bytes'),
                'tokens_per_second': be.get('tokens_per_second'), 'requests': st.get('requests'), 'errors': st.get('errors'), 'http': st.get('http'),
                'agents': self.gate.snapshot(), 'node_agents': st.get('agents'), 'pending': self.pending, 'error': '' if self.online else self.error,
                'last_seen': self.last_seen, 'allow_release': (st.get('config') or {}).get('allow_release_restart')}


class Fleet:
    def __init__(self, registry, settings, log=lambda *a, **k: None):
        self.registry, self.settings, self.log = registry, settings, log
        self.nodes = {}
        self.session = None
        self.metrics = collections.deque(maxlen=5000)   # (time, http status, ttft ms | None, console, agent)
        self.sticky = {}        # (agent, model) -> console id
        self.agents_seen = {}   # agent -> {"last": ts, "console": id, "requests": n}
        self._task = None
        self._seen_version = -1

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=1800))
        self.sync()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self.session:
            await self.session.close()

    def sync(self):
        """Make `nodes` match the registry (consoles can be added and removed while running)."""
        want = {c['id']: c for c in self.registry.list()}
        for cid in list(self.nodes):
            if cid not in want:
                del self.nodes[cid]
        for cid, c in want.items():
            if cid in self.nodes:
                self.nodes[cid].cfg = c
            else:
                self.nodes[cid] = Node(c)
        self._seen_version = self.registry.version

    async def _poll_loop(self):
        while True:
            try:
                if self._seen_version != self.registry.version:
                    self.sync()
                await asyncio.gather(*[self.poll(n) for n in list(self.nodes.values())], return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the poller must never die
                self.log('hub', f'poll error {type(exc).__name__}: {exc}')
            await asyncio.sleep(max(1, int(self.settings.values['poll_seconds'])))

    async def get(self, node, path, timeout=4):
        try:
            async with self.session.get(node.url + path, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                node.error = f'HTTP {r.status} from {path}'
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            node.error = f'{type(exc).__name__}'
        return None

    async def poll(self, node):
        st = await self.get(node, '/native/status', 3)
        was = node.online
        if isinstance(st, dict):
            node.status, node.online, node.last_seen, node.error = st, True, time.time(), ''
            if not was:
                self.log('hub', f'console {node.id} ({node.cfg["host"]}) is online: build {st.get("build")}, loaded {st.get("loaded") or "nothing"}')
            if time.time() - node.tags_at > 10 or not node.tags or (node.pending and time.time() - node.tags_at > 2):
                tg = await self.get(node, '/api/tags', 4)
                if isinstance(tg, dict):
                    node.tags, node.tags_at = tg.get('models', []), time.time()
            self._settle(node)
        else:
            node.online = False
            if was:
                self.log('hub', f'console {node.id} went offline ({node.error})')

    def _settle(self, node):
        p = node.pending
        if not p:
            return
        if p['action'] == 'load' and node.loaded == p['model'] or p['action'] == 'unload' and node.loaded == '' and time.time() - p['since'] > 1.5:
            self.log('hub', f'console {node.id}: {p["action"]} {p.get("model") or ""} finished')
            node.pending = None
        elif time.time() - p['since'] > 1200:
            self.log('hub', f'console {node.id}: {p["action"]} timed out')
            node.pending = None

    # ------------------------------------------------------------------ management
    def get_node(self, cid):
        n = self.nodes.get(cid)
        if not n:
            raise NoRoute(f'unknown console {cid!r}', 404)
        return n

    async def post(self, node, path, body, timeout=60):
        """(status, json|None); (0, error) if the console dropped the connection (it relaunches itself to switch models)."""
        try:
            async with self.session.post(node.url + path, json=body, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                try:
                    return r.status, await r.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    return r.status, None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return 0, type(exc).__name__

    async def load(self, cid, model):
        node = self.get_node(cid)
        if not node.online:
            raise NoRoute(f'console {cid} is offline', 502)
        if model not in node.installed():
            raise NoRoute(f'{model!r} is not installed on {cid}', 404)
        if node.loaded == model:
            return {'state': 'loaded'}
        node.pending = {'action': 'load', 'model': model, 'since': time.time()}
        self.log('hub', f'console {cid}: loading {model}')
        status, body = await self.post(node, '/native/load', {'model': model}, 900)
        if status == 200:
            node.status = {**(node.status or {}), 'loaded': model}
            node.pending = None
            return {'state': 'loaded'}
        if status in (0, 503) or (isinstance(body, dict) and 'swapping' in json.dumps(body)):
            return {'state': 'swapping'}     # the console relaunches into the model; the poller settles `pending`
        node.pending = None
        raise NoRoute(f'console {cid} refused the load (HTTP {status}): {json.dumps(body)[:200]}', 502)

    async def unload(self, cid):
        node = self.get_node(cid)
        if not node.online:
            raise NoRoute(f'console {cid} is offline', 502)
        if not node.loaded:
            return {'state': 'idle'}
        await self.post(node, '/native/config', {'allow_release_restart': True}, 10)
        node.pending = {'action': 'unload', 'model': '', 'since': time.time()}
        self.log('hub', f'console {cid}: unloading {node.loaded} (the app restarts to free the GPU)')
        await self.post(node, '/native/unload', {'release': True}, 60)
        return {'state': 'restarting'}

    # ------------------------------------------------------------------ routing
    def choose(self, model, agent, console=None):
        """The console that should serve `model` for `agent`. Raises NoRoute with an actionable message."""
        nodes = list(self.nodes.values())
        if not nodes:
            raise NoRoute('no console is registered: add one (dashboard -> Consoles, or `nlag consoles add <ip>`)', 503)
        if console:
            n = self.nodes.get(console)
            if not n:
                raise NoRoute(f'unknown console {console!r}', 404)
            if not n.online:
                raise NoRoute(f'console {console} is offline', 502)
            if model not in n.installed():
                raise NoRoute(f'{model!r} is not installed on {console}', 404)
            return n
        have = [n for n in nodes if n.online and model in n.installed()]
        if not have:
            raise NoRoute(f'model {model!r} is not installed on any online console', 404)
        loaded = [n for n in have if n.loaded == model and not (n.pending and n.pending['action'] == 'unload')]
        if loaded:
            key = (agent, model)
            if self.settings.values['agent_routing'] == 'sticky' and agent:
                pin = self.nodes.get(self.sticky.get(key, ''))
                if pin in loaded:
                    return pin
            best = min(loaded, key=lambda n: (n.gate.depth(), n.used))
            if agent:
                self.sticky[key] = best.id
            return best
        loading = [n for n in have if n.pending and n.pending['action'] == 'load' and n.pending['model'] == model]
        if loading:
            return loading[0]
        if self.settings.values['auto_load'] == 'idle-only':
            idle = [n for n in have if not n.loaded and not n.pending and n.gate.depth() == 0]
            if idle:
                return min(idle, key=lambda n: n.used)
        raise NoRoute(f'model {model!r} is installed on {", ".join(n.id for n in have)} but not loaded, and every console is busy with another model: load it from the dashboard, '
                      f'or address a console explicitly ({model}@{have[0].id})', 409)

    async def ensure_loaded(self, node, model, deadline=900):
        """Called while holding the console's gate."""
        if node.loaded == model and not node.pending:
            return
        if not (node.pending and node.pending['action'] == 'load' and node.pending['model'] == model):
            r = await self.load(node.id, model)
            if r['state'] == 'loaded':
                return
        end = time.time() + deadline
        while time.time() < end:
            await asyncio.sleep(1.5)
            if node.loaded == model and node.online and not node.pending:
                return
            if node.pending is None and node.loaded != model and node.online:
                raise NoRoute(f'console {node.id} did not load {model}', 502)
        raise NoRoute(f'timed out waiting for {node.id} to load {model}', 504)

    def record(self, status, ttft, node, agent):
        self.metrics.append((time.time(), status, ttft, node.id if node else '', agent))
        if agent:
            a = self.agents_seen.setdefault(agent, {'requests': 0})
            a.update({'last': time.time(), 'console': node.id if node else ''})
            a['requests'] += 1

    def metrics_summary(self, window_s=86400):
        cutoff = time.time() - window_s
        rows = [r for r in self.metrics if r[0] >= cutoff]
        ttfts = [r[2] for r in rows if r[2] is not None and r[1] < 400]
        return {'window_h': window_s // 3600, 'ok': sum(1 for r in rows if r[1] < 400), 'failed': sum(1 for r in rows if r[1] >= 400 and r[1] != 499),
                'ttft_ms_median': statistics.median(ttfts) if ttfts else None}

    # ------------------------------------------------------------------ proxy
    async def forward(self, request, node, path, body, public=None, bare=None, agent='', gated=True):
        """Serve one request on `node`: wait for the console's turn (round-robin across agents), make sure the model is loaded, stream the answer."""
        mark = {'t0': time.monotonic()}
        status = 500
        got_gate = False
        try:
            if gated:
                await node.gate.acquire(agent)
                got_gate = True
                node.used = time.time()
                if bare and node.loaded != bare:
                    await self.ensure_loaded(node, bare)
            resp = await self._proxy(request, node, path, body, public, bare, agent, mark)
            status = resp.status
            return resp
        except NoRoute as exc:
            status = exc.status
            return web.json_response({'error': {'message': str(exc), 'type': 'no_route'}}, status=exc.status)
        except (asyncio.CancelledError, ConnectionResetError):
            status = 499
            raise
        finally:
            if got_gate:
                node.gate.release()
            self.record(status, mark.get('ttft'), node, agent)

    async def _proxy(self, request, node, path, body, public, bare, agent, mark):
        headers = {k: v for k, v in request.headers.items() if k.lower() in ('content-type', 'accept')}
        if agent:
            headers['X-Agent-Id'] = agent
        url = node.url + path + (('?' + request.query_string) if request.query_string else '')
        try:
            upstream = await self.session.request(request.method, url, data=body, headers=headers, allow_redirects=False)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return web.json_response({'error': {'message': f'console {node.id} unreachable: {type(exc).__name__}', 'type': 'upstream'}}, status=502)
        try:
            out_headers = {k: v for k, v in upstream.headers.items() if k.lower() in protocol.PASS_HEADERS or k.lower().startswith('x-native-')}
            out_headers['X-NickLabs-Console'] = node.id
            ctype = upstream.headers.get('Content-Type', '')
            streaming = upstream.headers.get('Transfer-Encoding', '').lower() == 'chunked' or 'event-stream' in ctype or 'ndjson' in ctype
            if not streaming:
                raw = await upstream.read()
                if public and 'json' in ctype:
                    raw = protocol.rewrite_model_line(raw, public, bare)
                return web.Response(status=upstream.status, body=raw, headers=out_headers)
            resp = web.StreamResponse(status=upstream.status, headers={**out_headers, 'X-Accel-Buffering': 'no'})
            await resp.prepare(request)
            pending = b''
            try:
                async for chunk in upstream.content.iter_any():
                    if 'ttft' not in mark and protocol.FIRST_TEXT.search(chunk):
                        mark['ttft'] = (time.monotonic() - mark['t0']) * 1000.0
                    pending += chunk
                    *lines, pending = pending.split(b'\n')
                    for line in lines:
                        await resp.write((protocol.rewrite_model_line(line, public, bare) if public else line) + b'\n')
                if pending:
                    await resp.write(protocol.rewrite_model_line(pending, public, bare) if public else pending)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                err = json.dumps({'error': f'console {node.id} stream failed: {type(exc).__name__}'}).encode()
                await resp.write((b'data: ' + err + b'\n\n') if 'event-stream' in ctype else err + b'\n')
            await resp.write_eof()
            return resp
        finally:
            upstream.close()   # client gone or finished: closing the upstream socket makes the console cancel the generation
