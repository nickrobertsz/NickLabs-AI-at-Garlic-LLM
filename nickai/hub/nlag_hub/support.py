"""Reading the persistent logs and building a support bundle: one zip the user can attach to a bug report or hand to their AI agent.

Redaction (on by default) replaces what identifies the user's network or account with stable placeholders: IPv4/IPv6 addresses (<ip-1>), MAC addresses,
console host names, bearer tokens / API keys, PlayStation account ids ("userId = ..."), and the user's home path. It is best effort - read the bundle
before you publish it, and use --no-redact only for a private chat with your own tools."""
import io
import json
import os
import pathlib
import platform
import re
import sys
import time
import zipfile

from . import core

IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
IPV6 = re.compile(r'(?<![\w:])(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}(?![\w:])')
MAC = re.compile(r'(?<![\w:-])(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}(?![\w:-])')
TOKEN = re.compile(r'(?i)(bearer\s+|api[_-]?key["\']?\s*[:=]\s*["\']?|authorization:\s*)([A-Za-z0-9._~+/=-]{8,})')
USERID = re.compile(r'(?i)(userid\s*=\s*)(?:0x)?[0-9a-f]{4,}')
ACCOUNT = re.compile(r'\b[A-Za-z0-9_-]+(?=\(userId)')   # the console prints  <account name>(userId = <hex>)
HOME = re.compile(r'(/home/[^/\s]+|/Users/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)')
KEEP_IPS = {'0.0.0.0', '127.0.0.1', '255.255.255.255', '255.255.255.0'}


class Redactor:
    def __init__(self, hosts=()):
        self.map = {}
        self.hosts = [h for h in hosts if h]

    def alias(self, value, kind='ip'):
        if value not in self.map:
            self.map[value] = f'<{kind}-{len([k for k in self.map.values() if k.startswith("<" + kind)]) + 1}>'
        return self.map[value]

    def __call__(self, text):
        for h in self.hosts:
            text = text.replace(h, self.alias(h, 'host') if not IPV4.fullmatch(h) else self.alias(h))
        text = IPV4.sub(lambda m: m.group(0) if m.group(0) in KEEP_IPS or any(int(x) > 255 for x in m.group(0).split('.')) else self.alias(m.group(0)), text)
        text = IPV6.sub(lambda m: self.alias(m.group(0), 'ip6'), text)
        text = MAC.sub(lambda m: self.alias(m.group(0), 'mac'), text)
        text = TOKEN.sub(lambda m: m.group(1) + '<secret>', text)
        text = ACCOUNT.sub('<account-name>', text)
        text = USERID.sub(lambda m: m.group(1) + '<account>', text)
        text = HOME.sub('<home>', text)
        return text


def log_root():
    return core.DATA / 'logs'


def list_logs():
    out = []
    root = log_root()
    if root.is_dir():
        for p in sorted(root.rglob('*')):
            if p.is_file() and 'coredumps' not in p.parts:
                out.append({'path': str(p.relative_to(root)), 'bytes': p.stat().st_size, 'modified': int(p.stat().st_mtime)})
    return out


def tail(rel, lines=200):
    """Last `lines` lines of a log file (path relative to the log root; no escaping the root)."""
    p = (log_root() / rel).resolve()
    if log_root().resolve() not in p.parents or not p.is_file():
        raise FileNotFoundError(rel)
    with open(p, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block, data = 1 << 16, b''
        pos = size
        while pos > 0 and data.count(b'\n') <= lines:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return data.decode('utf-8', 'replace').splitlines()[-lines:]


def _tail_bytes(p, cap):
    size = p.stat().st_size
    with open(p, 'rb') as f:
        if size > cap:
            f.seek(size - cap)
            f.readline()
            return b'#### truncated: only the last %d bytes of %d are included\n' % (cap, size) + f.read()
        return f.read()


def build_bundle(registry, settings, fleet_state=None, jobs=None, redact=True, days=3, per_file_mb=20):
    """-> bytes of a zip. `fleet_state` is the dashboard state dict (consoles, metrics) and `jobs` the job list."""
    hosts = [c['host'] for c in registry.list()] + [c['name'] for c in registry.list() if c['name'] != c['id']]
    R = Redactor(hosts) if redact else (lambda t: t)
    cutoff = time.time() - days * 86400
    buf = io.BytesIO()
    cap = per_file_mb << 20
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        def add(name, text):
            z.writestr(name, R(text) if redact else text)
        info = {'app': core.APP_NAME, 'hub_version': core.VERSION, 'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'python': sys.version.split()[0],
                'platform': platform.platform(), 'redacted': redact, 'log_days_included': days}
        add('SUPPORT-INFO.json', json.dumps(info, indent=1))
        add('consoles.json', json.dumps([{k: v for k, v in c.items() if k != 'added'} for c in registry.list()], indent=1))
        add('settings.json', json.dumps(settings.public(), indent=1))
        if fleet_state is not None:
            add('state.json', json.dumps(fleet_state, indent=1, default=str))
        if jobs is not None:
            add('jobs.json', json.dumps(jobs, indent=1, default=str))
        root = log_root()
        if root.is_dir():
            for p in sorted(root.rglob('*')):
                if not p.is_file() or 'coredumps' in p.parts:
                    continue
                m = re.search(r'-(\d{8})\.log$', p.name)
                if m and p.stat().st_mtime < cutoff:
                    continue
                data = _tail_bytes(p, cap)
                text = data.decode('utf-8', 'replace')
                z.writestr('logs/' + str(p.relative_to(root)), R(text) if redact else text)
        names = [p.name for p in (root / 'coredumps').rglob('*') if p.is_file()] if (root / 'coredumps').is_dir() else []
        if names:
            add('COREDUMPS.txt', 'Kernel crash dumps exist on this computer (not included, they can be large and contain memory): ' + ', '.join(names))
        add('README.txt', 'NickLabs AI on Garlic LLM support bundle.\nSend this file to support, or give it to your AI agent and ask it to find why something failed.\n'
            'IP addresses, MAC addresses, tokens, account ids and your home path were replaced by placeholders (<ip-1>, <secret>, ...)' + ('' if redact else ' - NOT in this bundle (redaction was turned off)') + '.\n'
            'logs/<console>/klog-*.log is everything the console printed, events-*.log the interesting subset, boot.log/native.log are the app\'s own journals,\n'
            'logs/hub/ is what the hub did. Search for "ERROR", "FAIL", "SIG", "crash", "gpu".\n')
    return buf.getvalue()
