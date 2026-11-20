"""The hub web server: dashboard, management API (/hub/v1/*) and the Ollama / OpenAI / Anthropic compatible API in front of all consoles."""
import asyncio
import hmac
import json
import os
import pathlib
import time

from aiohttp import web

from . import core, ftp, models, protocol, sourcing, support
from .capture import Capture
from .discovery import Discovery
from .keys import KeyStore
from .fleet import Fleet, NoRoute

STATIC = pathlib.Path(__file__).resolve().parent / 'static'
PUBLIC_PATHS = {'/healthz', '/favicon.ico'}

REGISTRY = web.AppKey('registry', core.Registry)
SETTINGS = web.AppKey('settings', core.Settings)
FLEET = web.AppKey('fleet', Fleet)
JOBS = web.AppKey('jobs', models.Jobs)
CAPTURE = web.AppKey('capture', Capture)
KEYS = web.AppKey('keys', KeyStore)
DISCOVERY = web.AppKey('discovery', Discovery)


def jerr(message, status=400, **extra):
    return web.json_response({'error': {'message': message, **extra}}, status=status)


def pkg_info():
    d = pathlib.Path(os.environ.get('NLAG_DIST', pathlib.Path(__file__).resolve().parent.parent / 'dist'))
    meta = {}
    try:
        meta = json.loads((d / 'package.json').read_text())
    except (OSError, ValueError):
        pass
    pkg = next(iter(sorted(d.glob('*.pkg'))), None) if d.is_dir() else None
    return {'available': bool(pkg), 'path': str(pkg) if pkg else '', 'version': meta.get('version'), 'sha256': meta.get('sha256'), 'bytes': pkg.stat().st_size if pkg else 0}


# --------------------------------------------------------------------------------------------------------------------------- middleware
def is_inference_path(path):
    return path.startswith('/v1/') or (path.startswith('/api/') and path != '/api/')


@web.middleware
async def guard(request, handler):
    """Who may do what. Default: anyone on the trusted private networks, no key. A tool key (optionally bound to one agent) may use the AI endpoints from anywhere;
    only the admin key (or the trusted network, unless keys are required) may manage the hub."""
    st = request.app[SETTINGS]
    path = request.path
    request['agent_bound'] = ''
    if path in PUBLIC_PATHS or request.method == 'OPTIONS' or path == '/' or path.startswith('/static/'):
        return await handler(request)
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.startswith('Bearer ') else request.headers.get('x-api-key', '')
    admin = st.values.get('api_key') or ''
    rec = request.app[KEYS].authenticate(token) if token else None
    if admin and token and hmac.compare_digest(token, admin):
        pass
    elif rec:
        if not is_inference_path(path):
            return jerr('this key can only be used for AI requests (it cannot manage the hub)', 403)
        request['agent_bound'] = rec.get('agent') or ''
    elif st.values.get('require_keys'):
        return jerr('a key is required: create one in the dashboard (Settings -> Connect your tools) and send it as "Authorization: Bearer <key>"', 401)
    elif not st.trusted(request.remote or ''):
        return jerr('this address is not on a trusted network: send a key ("Authorization: Bearer <key>") or add the network in Settings', 403)
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except NoRoute as exc:
        return jerr(str(exc), exc.status)
    except (ValueError, KeyError, core.RegistryError, sourcing.SourceError) as exc:
        return jerr(str(exc), 400)
    except FileNotFoundError:
        return jerr('not found', 404)


async def body_json(request):
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------------------------------------------------------------------- dashboard
async def index(request):
    return web.FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-store'})


def state_snapshot(app):
    fleet, reg = app[FLEET], app[REGISTRY]
    nodes = [fleet.nodes[c['id']].summary() for c in reg.list() if c['id'] in fleet.nodes]
    return {'app': core.APP_NAME, 'version': core.VERSION, 'time': time.time(), 'consoles': nodes, 'metrics': fleet.metrics_summary(),
            'agents': {'seen': fleet.agents_seen, 'sticky': {f'{a}|{m}': c for (a, m), c in fleet.sticky.items()}},
            'jobs': app[JOBS].list(), 'library': models.library(), 'keys': app[KEYS].list(), 'discovery': {k: v for k, v in app[DISCOVERY].last.items() if k != 'apps'},
            'public_url': os.environ.get('NLAG_PUBLIC_URL', ''), 'settings': app[SETTINGS].public(), 'package': pkg_info(),
            'capture': {cid: {k: v for k, v in s.items() if k in ('klog_connected', 'klog_lines', 'ftp_ok', 'api_up', 'klog_error', 'ftp_error')} for cid, s in app[CAPTURE].status().items()}}


async def api_state(request):
    return web.json_response(state_snapshot(request.app))


# ------------------------------------------------------------------------------------------------------------------------------ consoles
async def probe_console(c, capture=None):
    """Reachability of the three things the hub uses on a console. The log port serves ONE reader: when the recorder is already connected it is not probed
    (a second connection would drop the recorder)."""
    import socket

    def tcp(port):
        try:
            with socket.create_connection((c['host'], port), timeout=3):
                return True
        except OSError as exc:
            return f'{type(exc).__name__}: {exc}'
    ok_ftp, detail = await asyncio.to_thread(ftp.probe, c)
    api = await asyncio.to_thread(tcp, c['api_port'])
    rec = capture.consoles.get(c['id']) if capture else None
    klog = True if rec and rec.st.get('klog_connected') else await asyncio.to_thread(tcp, c['klog_port'])
    return {'ftp': {'ok': ok_ftp, 'detail': detail}, 'app_api': {'ok': api is True, 'detail': '' if api is True else api}, 'klog': {'ok': klog is True, 'detail': '' if klog is True else klog}}


async def consoles_add(request):
    c = request.app[REGISTRY].add(await body_json(request))
    request.app[FLEET].sync()
    request.app[CAPTURE].log('hub', f'console added: {c["id"]} ({c["host"]})')
    return web.json_response({'console': c, 'probe': await probe_console(c, request.app[CAPTURE])}, status=201)


async def consoles_update(request):
    cid = request.match_info['cid']
    c = request.app[REGISTRY].update(cid, await body_json(request))
    request.app[FLEET].sync()
    return web.json_response({'console': c})


async def consoles_remove(request):
    cid = request.match_info['cid']
    c = request.app[REGISTRY].get(cid)
    request.app[REGISTRY].remove(cid)
    if c:   # do not let discovery bring back what the user removed
        ig = request.app[SETTINGS].values['ignored_hosts']
        request.app[SETTINGS].update({'ignored_hosts': sorted(set(ig) | {c['host']})})
    request.app[FLEET].sync()
    request.app[CAPTURE].log('hub', f'console removed: {cid}')
    return web.json_response({'removed': cid})


def console_or_404(request):
    c = request.app[REGISTRY].get(request.match_info['cid'])
    if not c:
        raise NoRoute('unknown console', 404)
    return c


async def consoles_check(request):
    return web.json_response(await probe_console(console_or_404(request), request.app[CAPTURE]))


async def consoles_load(request):
    c = console_or_404(request)
    model = (await body_json(request)).get('model')
    if not isinstance(model, str) or not model:
        return jerr('a model name is required')
    model, _ = protocol.split_model(model)
    return web.json_response(await request.app[FLEET].load(c['id'], model), status=202)


async def consoles_unload(request):
    c = console_or_404(request)
    return web.json_response(await request.app[FLEET].unload(c['id']), status=202)


async def consoles_install_pkg(request):
    c = console_or_404(request)
    pk = pkg_info()
    if not pk['available']:
        return jerr('this installation has no console package (dist/*.pkg)', 404)
    job = request.app[JOBS].start('install', f'send the app package to {c["name"]}', models.install_pkg_job(c, pk['path']))
    return web.json_response(job.to_json(), status=202)


async def consoles_unstage(request):
    c = console_or_404(request)
    name = request.match_info['model']
    if not sourcing.NAME_RE.match(name):
        return jerr('bad model name')
    node = request.app[FLEET].get_node(c['id'])
    if node.loaded == name:
        return jerr('unload the model first', 409)
    await asyncio.to_thread(ftp.unstage_bundle, c, name)
    return web.json_response({'removed': name})


# --------------------------------------------------------------------------------------------------------------------------------- models
async def ollama_list(request):
    items = await asyncio.to_thread(sourcing.list_ollama_local)
    return web.json_response({'stores': [str(p) for p in sourcing.ollama_dirs()], 'models': items})


async def models_import(request):
    d = await body_json(request)
    spec = d.get('source')
    if not isinstance(spec, str):
        return jerr('source is required: ollama-local:<name:tag>, ollama:<name:tag>, hf:<owner/repo>, path:<file>')
    reg = request.app[REGISTRY]
    push_to = [c for c in (d.get('push_to') or []) if isinstance(c, str)]
    for c in push_to:
        if not reg.get(c):
            return jerr(f'unknown console {c!r}', 404)
    fn = models.import_job(spec, d.get('name') or None, push_to, {c['id']: c for c in reg.list()})
    job = request.app[JOBS].start('import', f'import {spec}', fn)
    return web.json_response(job.to_json(), status=202)


async def library_list(request):
    return web.json_response({'models': models.library()})


async def library_delete(request):
    models.library_delete(request.match_info['mid'])
    return web.json_response({'removed': request.match_info['mid']})


async def library_push(request):
    mid = request.match_info['mid']
    d = await body_json(request)
    c = request.app[REGISTRY].get(d.get('console') or '')
    if not c:
        return jerr('console: pick the console to send the model to', 404)
    if not models.library_get(mid):
        return jerr(f'{mid!r} is not in the library', 404)
    job = request.app[JOBS].start('push', f'send {mid} to {c["name"]}', models.push_job(mid, c))
    return web.json_response(job.to_json(), status=202)


async def jobs_list(request):
    return web.json_response({'jobs': request.app[JOBS].list()})


async def jobs_get(request):
    j = request.app[JOBS].get(request.match_info['jid'])
    if not j:
        return jerr('unknown job', 404)
    return web.json_response(j.to_json(full=True))


async def jobs_cancel(request):
    j = request.app[JOBS].get(request.match_info['jid'])
    if not j:
        return jerr('unknown job', 404)
    j.cancel.set()
    return web.json_response({'cancelling': j.id})


# ------------------------------------------------------------------------------------------------------------------------ settings / logs
async def keys_list(request):
    return web.json_response({'keys': request.app[KEYS].list()})


async def keys_create(request):
    d = await body_json(request)
    token, rec = request.app[KEYS].create(d.get('name'), d.get('agent'))
    request.app[CAPTURE].log('hub', f'key created: {rec["name"]}' + (f' (agent {rec["agent"]})' if rec['agent'] else ''))
    return web.json_response({'key': token, 'record': rec}, status=201)


async def keys_revoke(request):
    request.app[KEYS].revoke(request.match_info['kid'])
    return web.json_response({'revoked': request.match_info['kid']})


async def discover_now(request):
    d = request.app[DISCOVERY]
    ig = set(request.app[SETTINGS].values['ignored_hosts'])
    if (await body_json(request)).get('forget_removed'):
        request.app[SETTINGS].update({'ignored_hosts': []})
        ig = set()
    res = await d.run(register=True)
    return web.json_response({k: v for k, v in res.items()})


async def settings_get(request):
    return web.json_response(request.app[SETTINGS].public())


async def settings_put(request):
    request.app[SETTINGS].update(await body_json(request))
    return web.json_response(request.app[SETTINGS].public())


async def logs_list(request):
    return web.json_response({'files': support.list_logs(), 'capture': request.app[CAPTURE].status()})


async def logs_tail(request):
    rel = request.query.get('path', '')
    n = max(1, min(2000, int(request.query.get('n', '200'))))
    return web.json_response({'path': rel, 'lines': await asyncio.to_thread(support.tail, rel, n)})


async def support_bundle(request):
    redact = request.query.get('redact', '1') not in ('0', 'false', 'no')
    app = request.app
    data = await asyncio.to_thread(support.build_bundle, app[REGISTRY], app[SETTINGS], state_snapshot(app), app[JOBS].list(), redact)
    name = time.strftime('nlag-support-%Y%m%d-%H%M%S.zip')
    return web.Response(body=data, headers={'Content-Type': 'application/zip', 'Content-Disposition': f'attachment; filename="{name}"'})


# --------------------------------------------------------------------------------------------------------------------- compat API (clients)
async def compat_tags(request):
    fleet = request.app[FLEET]
    merged = protocol.merge_tags([(n.id, {'models': n.tags}, n.loaded) for n in fleet.nodes.values() if n.online], with_consoles=request.query.get('all') in ('1', 'true'))
    if request.path == '/v1/models':
        return web.json_response(protocol.merge_openai_models(merged))
    return web.json_response(merged)


async def compat_ps(request):
    out = []
    for n in request.app[FLEET].nodes.values():
        if n.online and n.loaded:
            size = next((m.get('size') for m in n.tags if m.get('name') == n.loaded), 0)
            out.append({'name': n.loaded, 'model': n.loaded, 'size': size, 'size_vram': size, 'expires_at': '2999-12-31T23:59:59Z', 'context_length': (n.status or {}).get('ctx_limit'),
                        'nicklabs': {'console': n.id}})
    return web.json_response({'models': out})


async def compat_version(request):
    return web.json_response({'version': '0.9.0', 'nicklabs': core.VERSION})


async def compat_inference(request):
    raw = await request.read()
    try:
        body = json.loads(raw)
    except ValueError:
        return jerr('the request body must be JSON', 400)
    if not isinstance(body, dict):
        return jerr('the request body must be a JSON object', 400)
    public = body.get('model')
    model, console = protocol.split_model(public)
    if not isinstance(model, str) or not model:
        return jerr('"model" is required', 400)
    agent = request.get('agent_bound') or protocol.agent_of(request.headers, body)   # a key dedicated to an agent speaks as that agent only
    fleet = request.app[FLEET]
    try:
        node = fleet.choose(model, agent, console)
    except NoRoute as exc:
        return jerr(str(exc), exc.status, type='no_route')
    body['model'] = model
    gated = request.path not in ('/api/show', '/v1/messages/count_tokens')
    return await fleet.forward(request, node, request.path, json.dumps(body).encode(), public=public if public != model else None, bare=model, agent=agent, gated=gated)


async def not_found(request):
    return jerr(f'no such endpoint: {request.method} {request.path}', 404)


# ------------------------------------------------------------------------------------------------------------------------------ assembly
def create_app(data_dir=None):
    if data_dir:
        core.DATA = pathlib.Path(data_dir)
    core.DATA.mkdir(parents=True, exist_ok=True)
    app = web.Application(middlewares=[guard], client_max_size=64 << 20)
    registry, settings = core.Registry(), core.Settings()
    capture = Capture(registry, settings, core.DATA / 'logs')
    fleet = Fleet(registry, settings, log=lambda src, msg: capture.log(src, msg))
    jobs = models.Jobs(log=lambda src, msg: capture.log(src, msg))
    keys = KeyStore()
    discovery = Discovery(registry, settings, fleet, log=lambda src, msg: capture.log(src, msg))
    app[REGISTRY], app[SETTINGS], app[FLEET], app[JOBS], app[CAPTURE], app[KEYS], app[DISCOVERY] = registry, settings, fleet, jobs, capture, keys, discovery

    async def on_start(a):
        if os.environ.get('NLAG_CAPTURE', '1') != '0':   # tests/dev: never run a second reader against a console that something else already logs
            capture.start()
        await fleet.start()
        if os.environ.get('NLAG_DISCOVERY', '1') != '0':
            discovery.start()

    async def on_stop(a):
        discovery.stop()
        await fleet.stop()
        capture.close()
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    r = app.router
    r.add_get('/', index)
    r.add_static('/static/', STATIC)
    r.add_get('/healthz', lambda req: web.json_response({'ok': True}))
    r.add_get('/hub/v1/state', api_state)
    r.add_post('/hub/v1/consoles', consoles_add)
    r.add_put('/hub/v1/consoles/{cid}', consoles_update)
    r.add_delete('/hub/v1/consoles/{cid}', consoles_remove)
    r.add_get('/hub/v1/consoles/{cid}/check', consoles_check)
    r.add_post('/hub/v1/consoles/{cid}/load', consoles_load)
    r.add_post('/hub/v1/consoles/{cid}/unload', consoles_unload)
    r.add_post('/hub/v1/consoles/{cid}/install-pkg', consoles_install_pkg)
    r.add_delete('/hub/v1/consoles/{cid}/models/{model}', consoles_unstage)
    r.add_get('/hub/v1/ollama', ollama_list)
    r.add_post('/hub/v1/models/import', models_import)
    r.add_get('/hub/v1/library', library_list)
    r.add_delete('/hub/v1/library/{mid}', library_delete)
    r.add_post('/hub/v1/library/{mid}/push', library_push)
    r.add_get('/hub/v1/jobs', jobs_list)
    r.add_get('/hub/v1/jobs/{jid}', jobs_get)
    r.add_post('/hub/v1/jobs/{jid}/cancel', jobs_cancel)
    r.add_get('/hub/v1/keys', keys_list)
    r.add_post('/hub/v1/keys', keys_create)
    r.add_delete('/hub/v1/keys/{kid}', keys_revoke)
    r.add_post('/hub/v1/discover', discover_now)
    r.add_get('/hub/v1/settings', settings_get)
    r.add_put('/hub/v1/settings', settings_put)
    r.add_get('/hub/v1/logs', logs_list)
    r.add_get('/hub/v1/logs/tail', logs_tail)
    r.add_get('/hub/v1/support-bundle', support_bundle)
    for p in ('/api/tags', '/v1/models'):
        r.add_get(p, compat_tags)
    r.add_get('/api/ps', compat_ps)
    r.add_get('/api/version', compat_version)
    for p in ('/api/chat', '/api/generate', '/api/show', '/v1/chat/completions', '/v1/completions', '/v1/messages', '/v1/messages/count_tokens', '/api/embed', '/api/embeddings', '/v1/embeddings'):
        r.add_post(p, compat_inference)
    r.add_route('*', '/{tail:.*}', not_found)
    return app


def main():
    import argparse
    ap = argparse.ArgumentParser(description='NickLabs AI on Garlic LLM hub')
    ap.add_argument('--host', default=os.environ.get('NLAG_HOST', '0.0.0.0'))
    ap.add_argument('--port', type=int, default=int(os.environ.get('NLAG_PORT', '8080')))
    ap.add_argument('--data', default=None)
    a = ap.parse_args()
    web.run_app(create_app(a.data), host=a.host, port=a.port, access_log=None, print=lambda *_: None)


if __name__ == '__main__':
    main()
