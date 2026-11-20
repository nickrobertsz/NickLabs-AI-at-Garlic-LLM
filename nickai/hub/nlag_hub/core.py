"""Paths, the console registry and the hub settings. No console is built in: a console is whatever the user registers (any number of them)."""
import ipaddress
import json
import os
import pathlib
import re
import threading
import time

DATA = pathlib.Path(os.environ.get('NLAG_DATA', '/data'))
ASSETS = pathlib.Path(os.environ.get('NLAG_ASSETS', pathlib.Path(__file__).resolve().parent.parent / 'assets'))
VERSION = '1.0.0'
APP_NAME = 'NickLabs AI on Garlic LLM'

ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,30}$')
HOST_RE = re.compile(r'^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$')


class RegistryError(ValueError):
    pass


def valid_host(host):
    if not isinstance(host, str) or not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(HOST_RE.match(host))


def atomic_write(path, text):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)


class Registry:
    """consoles.json: [{"id", "name", "host", "api_port", "ftp_port", "klog_port"}]. Thread-safe; changes are visible to every subsystem on its next look."""

    def __init__(self, path=None):
        self.path = pathlib.Path(path or DATA / 'consoles.json')
        self.lock = threading.RLock()
        self.items = {}
        self.version = 0
        self.load()

    def load(self):
        with self.lock:
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                data = []
            self.items = {}
            for c in data if isinstance(data, list) else []:
                try:
                    self.items[c['id']] = self._clean(c, allow_existing=True)
                except (RegistryError, KeyError, TypeError):
                    continue

    def _save(self):
        atomic_write(self.path, json.dumps(list(self.items.values()), indent=1) + '\n')
        self.version += 1

    def _clean(self, c, allow_existing=False, current=None):
        if not isinstance(c, dict):
            raise RegistryError('a console is an object')
        cid = str(c.get('id') or '').strip().lower()
        if not ID_RE.match(cid):
            raise RegistryError('id: 1-31 characters, lowercase letters, digits, - and _')
        host = str(c.get('host') or '').strip()
        if not valid_host(host):
            raise RegistryError('host must be an IP address or a host name')
        ports = {}
        for key, default in (('api_port', 11434), ('ftp_port', 2121), ('klog_port', 3232)):
            try:
                v = int(c.get(key, default))
            except (TypeError, ValueError):
                raise RegistryError(f'{key} must be a number')
            if not 1 <= v <= 65535:
                raise RegistryError(f'{key} out of range')
            ports[key] = v
        name = str(c.get('name') or cid).strip()[:60]
        return {'id': cid, 'name': name, 'host': host, **ports, 'added': c.get('added') or int(time.time())}

    def list(self):
        with self.lock:
            return [dict(c) for c in self.items.values()]

    def get(self, cid):
        with self.lock:
            c = self.items.get(cid)
            return dict(c) if c else None

    def next_id(self):
        with self.lock:
            n = 1
            while f'ps4-{n}' in self.items:
                n += 1
            return f'ps4-{n}'

    def add(self, c):
        with self.lock:
            c = dict(c)
            c.setdefault('id', self.next_id())
            clean = self._clean(c)
            if clean['id'] in self.items:
                raise RegistryError(f"a console with id '{clean['id']}' already exists")
            for o in self.items.values():
                if o['host'] == clean['host'] and o['api_port'] == clean['api_port']:
                    raise RegistryError(f"{clean['host']}:{clean['api_port']} is already registered as '{o['id']}'")
            self.items[clean['id']] = clean
            self._save()
            return dict(clean)

    def update(self, cid, changes):
        with self.lock:
            cur = self.items.get(cid)
            if not cur:
                raise RegistryError('unknown console')
            merged = {**cur, **{k: v for k, v in (changes or {}).items() if k in ('name', 'host', 'api_port', 'ftp_port', 'klog_port')}, 'id': cid}
            self.items[cid] = self._clean(merged, current=cur)
            self._save()
            return dict(self.items[cid])

    def remove(self, cid):
        with self.lock:
            if cid not in self.items:
                raise RegistryError('unknown console')
            del self.items[cid]
            self._save()


DEFAULT_SETTINGS = {
    'trusted_networks': ['127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fc00::/7'],   # who may use the dashboard/API without a key
    'api_key': '',                                    # the ADMIN key: manages the hub from outside the trusted networks (or when keys are required)
    'require_keys': False,                            # True: tools must present a key even on the trusted networks (needs an admin key)
    'auto_discovery': True,                           # find consoles running the app on the local network and register them
    'ignored_hosts': [],                              # consoles the user removed: discovery does not register them again
    'agent_routing': 'sticky',                        # sticky: an agent keeps its console while that console serves its model; spread: always the least busy
    'auto_load': 'idle-only',                         # idle-only: a request for an installed model may load it on an idle console; never: only on explicit @console
    'log_retention_days': 30,
    'log_max_mb': 500,
    'poll_seconds': 3,
}


class Settings:
    def __init__(self, path=None):
        self.path = pathlib.Path(path or DATA / 'settings.json')
        self.lock = threading.RLock()
        try:
            cur = json.loads(self.path.read_text())
        except (OSError, ValueError):
            cur = {}
        self.values = {**DEFAULT_SETTINGS, **({k: v for k, v in cur.items() if k in DEFAULT_SETTINGS} if isinstance(cur, dict) else {})}
        for k in ('NLAG_API_KEY',):   # an environment key wins (so a compose file can set it)
            if os.environ.get(k):
                self.values['api_key'] = os.environ[k]
        self.networks = self._nets()

    def _nets(self):
        out = []
        for n in self.values['trusted_networks']:
            try:
                out.append(ipaddress.ip_network(n, strict=False))
            except ValueError:
                continue
        return out

    def trusted(self, ip):
        try:
            addr = ipaddress.ip_address(ip.split('%')[0])
        except ValueError:
            return False
        return any(addr in n for n in self.networks)

    def update(self, changes):
        with self.lock:
            for k, v in (changes or {}).items():
                if k not in DEFAULT_SETTINGS:
                    raise RegistryError(f'unknown setting {k}')
                if k == 'trusted_networks':
                    if not isinstance(v, list):
                        raise RegistryError('trusted_networks is a list')
                    for n in v:
                        ipaddress.ip_network(n, strict=False)
                elif k in ('log_retention_days', 'log_max_mb', 'poll_seconds'):
                    v = int(v)
                    if v < 1:
                        raise RegistryError(f'{k} must be positive')
                elif k in ('require_keys', 'auto_discovery'):
                    v = bool(v)
                elif k == 'ignored_hosts':
                    if not isinstance(v, list) or not all(valid_host(x) for x in v):
                        raise RegistryError('ignored_hosts is a list of addresses')
                elif k in ('agent_routing', 'auto_load'):
                    if v not in {'agent_routing': ('sticky', 'spread'), 'auto_load': ('idle-only', 'never')}[k]:
                        raise RegistryError(f'bad value for {k}')
                self.values[k] = v
            if self.values['require_keys'] and not self.values['api_key']:
                self.values['require_keys'] = False
                raise RegistryError('set an admin key first: with required keys, only a key can open this dashboard')
            atomic_write(self.path, json.dumps({k: v for k, v in self.values.items() if k != 'api_key' or not os.environ.get('NLAG_API_KEY')}, indent=1) + '\n')
            self.networks = self._nets()

    def public(self):
        v = dict(self.values)
        v['api_key_set'] = bool(v.pop('api_key'))
        return v
