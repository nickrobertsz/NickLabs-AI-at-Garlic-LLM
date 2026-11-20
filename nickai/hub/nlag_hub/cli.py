"""nlag - the terminal control of NickLabs AI on Garlic LLM (standard library only, same on Linux, macOS and Windows).

  nlag status                              consoles, loaded models, who is speaking
  nlag setup                               guided first-time setup (add a console, send the app, add a model)
  nlag consoles [add IP [--name N] | remove ID | check ID | install ID | scan | forget]
  nlag models                              models on this computer and on every console
  nlag ollama                              models found in your local Ollama
  nlag pull SOURCE [--to ID,ID] [--name N] download/convert a model: qwen3.5:4b (Ollama registry), hf:Qwen/Qwen3.5-4B, ollama-local:qwen3.5:4b, path:/import/x.gguf
  nlag push MODEL CONSOLE                  send a model from this computer to a console
  nlag load MODEL CONSOLE | nlag unload CONSOLE
  nlag rm MODEL [CONSOLE]                  delete a model from this computer (or from a console)
  nlag jobs [watch ID | cancel ID]
  nlag chat MODEL[@CONSOLE] "text" [--agent NAME]
  nlag agents
  nlag logs [FILE] [-n 200]                list / show saved logs;  nlag logs bundle [--no-redact] [-o FILE]  = support bundle
  nlag settings [key=value ...]
The hub address comes from $NLAG_URL (default http://localhost:8080); an API key from $NLAG_KEY."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get('NLAG_URL', 'http://localhost:8080').rstrip('/')


def call(method, path, body=None, raw=False, timeout=60):
    headers = {'Content-Type': 'application/json'}
    if os.environ.get('NLAG_KEY'):
        headers['Authorization'] = 'Bearer ' + os.environ['NLAG_KEY']
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return data if raw else json.loads(data or b'null')
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get('error', {})
            msg = msg.get('message') if isinstance(msg, dict) else msg
        except ValueError:
            msg = e.reason
        sys.exit(f'error: {msg} (HTTP {e.code})')
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f'cannot reach the hub at {BASE}: {getattr(e, "reason", e)}\nIs it running? Start it with ./launch.sh (Windows: launch.bat).')


def gb(n):
    return f'{(n or 0) / 1e9:.2f} GB'


def table(rows, head):
    cols = [max(len(str(x)) for x in [h] + [r[i] for r in rows]) for i, h in enumerate(head)]
    print('  '.join(h.ljust(c) for h, c in zip(head, cols)))
    for r in rows:
        print('  '.join(str(x).ljust(c) for x, c in zip(r, cols)))


def cmd_status(a):
    s = call('GET', '/hub/v1/state')
    print(f'{s["app"]} {s["version"]}')
    if not s['consoles']:
        print('No console registered yet: run `nlag setup`.')
    rows = []
    for c in s['consoles']:
        ag = c.get('agents') or {}
        st = 'offline' if not c['online'] else ('loading' if (c.get('pending') or {}).get('action') == 'load' else 'unloading' if c.get('pending') else 'ready' if c['loaded'] else 'idle')
        rows.append((c['id'], c['name'], c['host'], st, c['loaded'] or '-', f'{c["tokens_per_second"]:.1f}' if c.get('tokens_per_second') else '-', ag.get('speaking') or '-', ','.join(f'{k}x{v}' for k, v in (ag.get('queued') or {}).items()) or '-'))
    if rows:
        table(rows, ('ID', 'NAME', 'ADDRESS', 'STATE', 'MODEL', 'TOK/S', 'SPEAKING', 'WAITING'))
    m = s['metrics']
    print(f'\nlast 24h: {m["ok"]} ok / {m["failed"]} failed requests' + (f', median first token {m["ttft_ms_median"]:.0f} ms' if m['ttft_ms_median'] is not None else ''))
    running = [j for j in s['jobs'] if j['state'] in ('queued', 'running')]
    for j in running:
        print(f'job {j["id"]}: {j["title"]} {j["progress"] * 100:.0f}% - {j["message"]}')


def cmd_consoles(a):
    if a.action in (None, 'list'):
        return cmd_status(a)
    if a.action == 'add':
        body = {'host': a.arg, 'name': a.name, 'api_port': a.api_port, 'ftp_port': a.ftp_port, 'klog_port': a.klog_port}
        r = call('POST', '/hub/v1/consoles', {k: v for k, v in body.items() if v})
        p = r['probe']
        print(f'added {r["console"]["id"]} ({r["console"]["host"]})')
        print(f'  FTP      {"ok" if p["ftp"]["ok"] else "NOT reachable: enable the FTP server on the console (GoldHEN, port 2121)"}')
        print(f'  app      {"running" if p["app_api"]["ok"] else "not running yet: install and start the app (nlag consoles install " + r["console"]["id"] + ")"}')
        print(f'  log port {"ok" if p["klog"]["ok"] else "not reachable (GoldHEN klog server)"}')
    elif a.action == 'remove':
        print(call('DELETE', f'/hub/v1/consoles/{a.arg}'))
    elif a.action == 'check':
        for k, v in call('GET', f'/hub/v1/consoles/{a.arg}/check').items():
            print(f'{k:8} {"ok" if v["ok"] else "NOT reachable: " + v["detail"]}')
    elif a.action in ('scan', 'forget'):
        r = call('POST', '/hub/v1/discover', {'forget_removed': a.action == 'forget'}, timeout=120)
        print(f'searched {", ".join(r["networks"]) or "no network"}: {len(r["apps"])} console(s) running the app, {len(r["candidates"])} possible console(s) without it' + (f', added {", ".join(r["added"])}' if r.get('added') else ''))
        for x in r['candidates']:
            print(f'  possible console without the app: {x["host"]}  ->  nlag consoles add {x["host"]}')
    elif a.action == 'install':
        j = call('POST', f'/hub/v1/consoles/{a.arg}/install-pkg')
        watch(j['id'])
        print('\nNow on the console: Settings -> Debug Settings -> Game -> Package Installer -> NickLabs AI on Garlic LLM, then start it.')


def cmd_models(a):
    s = call('GET', '/hub/v1/state')
    print('On this computer:')
    table([(m['id'], gb(m['bytes']), m['arch'], m['origin']) for m in s['library']] or [('-', '', '', 'nothing yet: nlag pull qwen3.5:4b')], ('MODEL', 'SIZE', 'ARCH', 'ORIGIN'))
    print('\nOn the consoles:')
    table([(c['id'], m['name'], gb(m['size']), 'loaded' if m['loaded'] else '') for c in s['consoles'] for m in c['models']] or [('-', '-', '', '')], ('CONSOLE', 'MODEL', 'SIZE', ''))


def cmd_ollama(a):
    d = call('GET', '/hub/v1/ollama')
    print('Ollama folders:', ', '.join(d['stores']) or 'none visible to the hub')
    table([(m['ref'], gb(m['bytes']), m['arch'], 'ok' if m['supported'] else m['reason'][:60]) for m in d['models']] or [('-', '', '', '')], ('MODEL', 'SIZE', 'ARCH', 'USABLE'))


def watch(jid, quiet=False):
    last = ''
    while True:
        j = call('GET', f'/hub/v1/jobs/{jid}')
        line = f'{j["state"]:9} {j["progress"] * 100:5.1f}%  {j["message"]}'
        if line != last and not quiet:
            print(line)
            last = line
        if j['state'] in ('done', 'failed', 'cancelled'):
            if j['state'] == 'failed':
                sys.exit(f'failed: {j["message"]}')
            return j
        time.sleep(2)


def cmd_pull(a):
    src = a.source
    if not src.startswith(('ollama:', 'ollama-local:', 'hf:', 'path:')):
        src = 'ollama:' + src
    to = [x for x in (a.to or '').split(',') if x]
    j = call('POST', '/hub/v1/models/import', {'source': src, 'name': a.name, 'push_to': to})
    print(f'job {j["id"]}: {j["title"]}')
    watch(j['id'])


def cmd_push(a):
    watch(call('POST', f'/hub/v1/library/{a.model}/push', {'console': a.console})['id'])


def cmd_load(a):
    print(call('POST', f'/hub/v1/consoles/{a.console}/load', {'model': a.model}))


def cmd_unload(a):
    print(call('POST', f'/hub/v1/consoles/{a.console}/unload'))


def cmd_rm(a):
    print(call('DELETE', f'/hub/v1/consoles/{a.console}/models/{a.model}') if a.console else call('DELETE', f'/hub/v1/library/{a.model}'))


def cmd_jobs(a):
    if a.action == 'watch':
        return watch(a.id)
    if a.action == 'cancel':
        return print(call('POST', f'/hub/v1/jobs/{a.id}/cancel'))
    table([(j['id'], j['state'], f'{j["progress"] * 100:.0f}%', j['title'], j['message'][:70]) for j in call('GET', '/hub/v1/jobs')['jobs']] or [('-', '', '', 'no jobs', '')], ('ID', 'STATE', '%', 'JOB', 'MESSAGE'))


def cmd_chat(a):
    body = json.dumps({'model': a.model, 'messages': [{'role': 'user', 'content': a.text}], 'stream': True, 'max_tokens': a.max_tokens}).encode()
    headers = {'Content-Type': 'application/json', **({'X-Agent-Id': a.agent} if a.agent else {}), **({'Authorization': 'Bearer ' + os.environ['NLAG_KEY']} if os.environ.get('NLAG_KEY') else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + '/v1/chat/completions', data=body, headers=headers), timeout=900) as r:
            sys.stderr.write(f'[{r.headers.get("X-NickLabs-Console")}] ')
            for line in r:
                line = line.decode().strip()
                if line.startswith('data: ') and line != 'data: [DONE]':
                    d = json.loads(line[6:]).get('choices', [{}])[0].get('delta', {}).get('content')
                    if d:
                        sys.stdout.write(d)
                        sys.stdout.flush()
            print()
    except urllib.error.HTTPError as e:
        sys.exit('error: ' + json.loads(e.read()).get('error', {}).get('message', str(e)))


def cmd_agents(a):
    s = call('GET', '/hub/v1/state')
    table([(c['id'], (c.get('agents') or {}).get('speaking') or '-', ','.join(f'{k}x{v}' for k, v in ((c.get('agents') or {}).get('queued') or {}).items()) or '-') for c in s['consoles']], ('CONSOLE', 'SPEAKING', 'WAITING'))
    print()
    table([(k, v['console'], v['requests'], f'{time.time() - v["last"]:.0f}s ago') for k, v in sorted(s['agents']['seen'].items(), key=lambda kv: -kv[1]['last'])] or [('-', '', '', '')], ('AGENT', 'CONSOLE', 'REQUESTS', 'LAST'))


def cmd_logs(a):
    if a.file == 'bundle':
        data = call('GET', '/hub/v1/support-bundle' + ('?redact=0' if a.no_redact else ''), raw=True, timeout=300)
        out = a.output or time.strftime('nlag-support-%Y%m%d-%H%M%S.zip')
        open(out, 'wb').write(data)
        print(f'saved {out} ({len(data) / 1e6:.1f} MB): send it to support or give it to your AI agent' + ('' if a.no_redact else ' (IPs, tokens and account ids are hidden)'))
    elif a.file:
        print('\n'.join(call('GET', f'/hub/v1/logs/tail?path={urllib.request.quote(a.file)}&n={a.n}')['lines']))
    else:
        d = call('GET', '/hub/v1/logs')
        table([(f['path'], f'{f["bytes"] / 1e6:.2f} MB') for f in d['files']] or [('no logs yet', '')], ('FILE', 'SIZE'))


def cmd_settings(a):
    if a.pairs:
        body = {}
        for p in a.pairs:
            k, _, v = p.partition('=')
            body[k] = [x for x in v.split(',') if x] if k == 'trusted_networks' else (int(v) if v.isdigit() else v)
        print(json.dumps(call('PUT', '/hub/v1/settings', body), indent=1))
    else:
        print(json.dumps(call('GET', '/hub/v1/settings'), indent=1))


def ask(q, default=''):
    try:
        v = input(f'{q}{" [" + default + "]" if default else ""}: ').strip()
    except EOFError:
        v = ''
    return v or default


def cmd_setup(a):
    print('NickLabs AI on Garlic LLM - setup\n')
    s = call('GET', '/hub/v1/state')
    cons = s['consoles']
    if cons:
        print('Consoles already registered:', ', '.join(f'{c["id"]} ({c["host"]})' for c in cons))
    while True:
        if a.ip:
            ip, a.ip = a.ip, None
        else:
            ip = ask('IP address of a ' + ('PlayStation 4 to add' if not cons else 'further console to add') + ' (empty to continue)')
        if not ip:
            break
        r = call('POST', '/hub/v1/consoles', {'host': ip, 'name': ask('Name for it', ip)})
        c, p = r['console'], r['probe']
        while not p['ftp']['ok']:
            print('\nThe console does not answer on its FTP port (2121).\n  - Is it on and on the same network as this computer?\n  - Enable the FTP server (GoldHEN: Settings -> FTP server, or your jailbreak menu).')
            if ask('Try again? (Y/n)', 'y').lower() != 'y':
                break
            p = call('GET', f'/hub/v1/consoles/{c["id"]}/check')
        if p['app_api']['ok']:
            print(f'{c["id"]}: the app is already running there.')
        elif p['ftp']['ok'] and s['package']['available'] and ask('Send the app to this console now? (Y/n)', 'y').lower() == 'y':
            watch(call('POST', f'/hub/v1/consoles/{c["id"]}/install-pkg')['id'])
            print('\nOn the console: Settings -> Debug Settings -> Game -> Package Installer -> "NickLabs AI on Garlic LLM" -> install, then start it from the home screen.')
            ask('Press Enter when it is running')
            for _ in range(40):
                if call('GET', '/hub/v1/state')['consoles'] and any(x['id'] == c['id'] and x['online'] for x in call('GET', '/hub/v1/state')['consoles']):
                    print('the app answers.')
                    break
                time.sleep(3)
            else:
                print('the app does not answer yet: check the console, then run `nlag status`.')
        cons = call('GET', '/hub/v1/state')['consoles']
    s = call('GET', '/hub/v1/state')
    if not s['library'] and s['consoles']:
        ol = [m for m in call('GET', '/hub/v1/ollama')['models'] if m['supported'] and m['bytes'] < 6e9]
        if ol:
            print('\nQwen3.5 models found in your Ollama: ' + ', '.join(m['ref'] for m in ol))
            ref = ask('Convert which one for the consoles? (empty to skip)', ol[0]['ref'])
            if ref:
                watch(call('POST', '/hub/v1/models/import', {'source': 'ollama-local:' + ref, 'push_to': [c['id'] for c in s['consoles']]})['id'])
        elif ask('\nDownload Qwen3.5-4B from the Ollama registry (2.6 GB) and send it to your console(s)? (Y/n)', 'y').lower() == 'y':
            watch(call('POST', '/hub/v1/models/import', {'source': 'ollama:qwen3.5:4b', 'push_to': [c['id'] for c in s['consoles']]})['id'])
    print(f'\nDone. Open the dashboard: {BASE}\nThen load a model on a console (dashboard -> Consoles, or `nlag load <model> <console>`).')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='nlag', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('status').set_defaults(f=cmd_status)
    p = sub.add_parser('setup'); p.add_argument('--ip'); p.set_defaults(f=cmd_setup)
    p = sub.add_parser('consoles'); p.add_argument('action', nargs='?', choices=['list', 'add', 'remove', 'check', 'install', 'scan', 'forget']); p.add_argument('arg', nargs='?'); p.add_argument('--name')
    p.add_argument('--api-port', type=int); p.add_argument('--ftp-port', type=int); p.add_argument('--klog-port', type=int); p.set_defaults(f=cmd_consoles)
    sub.add_parser('models').set_defaults(f=cmd_models)
    sub.add_parser('ollama').set_defaults(f=cmd_ollama)
    p = sub.add_parser('pull'); p.add_argument('source'); p.add_argument('--to'); p.add_argument('--name'); p.set_defaults(f=cmd_pull)
    p = sub.add_parser('push'); p.add_argument('model'); p.add_argument('console'); p.set_defaults(f=cmd_push)
    p = sub.add_parser('load'); p.add_argument('model'); p.add_argument('console'); p.set_defaults(f=cmd_load)
    p = sub.add_parser('unload'); p.add_argument('console'); p.set_defaults(f=cmd_unload)
    p = sub.add_parser('rm'); p.add_argument('model'); p.add_argument('console', nargs='?'); p.set_defaults(f=cmd_rm)
    p = sub.add_parser('jobs'); p.add_argument('action', nargs='?', choices=['list', 'watch', 'cancel']); p.add_argument('id', nargs='?'); p.set_defaults(f=cmd_jobs)
    p = sub.add_parser('chat'); p.add_argument('model'); p.add_argument('text'); p.add_argument('--agent'); p.add_argument('--max-tokens', type=int, default=256); p.set_defaults(f=cmd_chat)
    sub.add_parser('agents').set_defaults(f=cmd_agents)
    p = sub.add_parser('logs'); p.add_argument('file', nargs='?'); p.add_argument('-n', type=int, default=200); p.add_argument('--no-redact', action='store_true'); p.add_argument('-o', '--output'); p.set_defaults(f=cmd_logs)
    p = sub.add_parser('settings'); p.add_argument('pairs', nargs='*'); p.set_defaults(f=cmd_settings)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    if a.cmd == 'consoles' and a.action in ('add', 'remove', 'check', 'install') and not a.arg:
        sys.exit(f'usage: nlag consoles {a.action} {"IP" if a.action == "add" else "ID"}')
    if a.cmd == 'jobs' and a.action in ('watch', 'cancel') and not a.id:
        sys.exit(f'usage: nlag jobs {a.action} ID')
    a.f(a)
    return 0


if __name__ == '__main__':
    sys.exit(main())
