"""Find the consoles on the local network and register them automatically.

A console that runs the app answers `GET :11434/native/status` with {"app": "nicklabs-native", ...}: those are registered without asking anything.
A console that only has the jailbreak's FTP (2121) and log (3232) servers open is listed as a candidate (the app is not installed there yet).

Inside Docker the hub cannot see the host's LAN by itself, so the launcher tells it which network(s) to scan (NLAG_SCAN_NETS, e.g. 192.168.1.0/24);
without it the hub scans the network of its own address when that is a private LAN address (running without Docker)."""
import asyncio
import ipaddress
import json
import os
import socket
import time

import aiohttp

API_PORT, FTP_PORT, KLOG_PORT = 11434, 2121, 3232
MAX_HOSTS = 1024


def default_networks():
    """Networks to scan: $NLAG_SCAN_NETS (comma separated CIDRs) or the /24 of this machine's LAN address."""
    env = os.environ.get('NLAG_SCAN_NETS', '').strip()
    nets = []
    if env:
        for part in env.split(','):
            try:
                nets.append(ipaddress.ip_network(part.strip(), strict=False))
            except ValueError:
                continue
        return nets
    try:   # the address used to reach the internet (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.0.2.1', 9))
        ip = ipaddress.ip_address(s.getsockname()[0])
        s.close()
        if ip.is_private and not ip.is_loopback and not str(ip).startswith('172.17.'):   # not Docker's own bridge
            nets.append(ipaddress.ip_network(f'{ip}/24', strict=False))
    except OSError:
        pass
    return nets


async def _open(host, port, timeout=0.5):
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        try:
            await w.wait_closed()
        except OSError:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def scan(networks, session, api_port=API_PORT, concurrency=200, skip=()):
    """-> {"apps": [{host, port, build, loaded}], "candidates": [{host}]}"""
    hosts = []
    for n in networks:
        for h in n.hosts():
            if str(h) in skip:      # registered consoles are never probed again: connecting to a console's log port would drop the log recorder
                continue
            hosts.append(str(h))
            if len(hosts) >= MAX_HOSTS:
                break
    sem = asyncio.Semaphore(concurrency)
    apps, cands = [], []

    async def probe(h):
        async with sem:
            if await _open(h, api_port):
                try:
                    async with session.get(f'http://{h}:{api_port}/native/status', timeout=aiohttp.ClientTimeout(total=3)) as r:
                        st = await r.json(content_type=None)
                    if isinstance(st, dict) and st.get('app') == 'nicklabs-native':
                        apps.append({'host': h, 'port': api_port, 'build': st.get('build'), 'loaded': st.get('loaded') or ''})
                        return
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
            if await _open(h, FTP_PORT) and await _open(h, KLOG_PORT):
                cands.append({'host': h})
    await asyncio.gather(*[probe(h) for h in hosts])
    key = lambda x: tuple(int(p) for p in x['host'].split('.'))   # noqa: E731
    return {'apps': sorted(apps, key=key), 'candidates': sorted(cands, key=key), 'scanned': len(hosts)}


class Discovery:
    def __init__(self, registry, settings, fleet, log=lambda *a, **k: None):
        self.registry, self.settings, self.fleet, self.log = registry, settings, fleet, log
        self.last = {'time': 0, 'networks': [], 'apps': [], 'candidates': [], 'scanned': 0, 'running': False}
        self._task = None
        self._lock = asyncio.Lock()
        self.api_port = API_PORT

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        await asyncio.sleep(2)
        while True:
            try:
                if self.settings.values.get('auto_discovery', True):
                    await self.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - discovery must never take the hub down
                self.log('discovery', f'error {type(exc).__name__}: {exc}')
            await asyncio.sleep(60)

    async def run(self, register=True):
        async with self._lock:
            nets = default_networks()
            registered = {c['host'] for c in self.registry.list()}
            self.last.update(running=True, networks=[str(n) for n in nets])
            try:
                res = await scan(nets, self.fleet.session, api_port=self.api_port, skip=registered) if nets else {'apps': [], 'candidates': [], 'scanned': 0}
            finally:
                self.last['running'] = False
            known = {(c['host'], c['api_port']) for c in self.registry.list()}
            ignored = set(self.settings.values.get('ignored_hosts') or [])
            added = []
            if register:
                for a in res['apps']:
                    if (a['host'], a['port']) in known or a['host'] in ignored:
                        continue
                    try:
                        c = self.registry.add({'host': a['host'], 'api_port': a['port'], 'name': f'PS4 {a["host"]}'})
                    except Exception:  # noqa: BLE001 - e.g. id clash: try again next round
                        continue
                    added.append(c)
                    self.log('discovery', f'found a console running the app at {a["host"]}: registered as {c["id"]}')
                if added:
                    self.fleet.sync()
            reg_hosts = {c['host'] for c in self.registry.list()}
            res['candidates'] = [c for c in res['candidates'] if c['host'] not in reg_hosts]
            self.last.update(time=time.time(), apps=res['apps'], candidates=res['candidates'], scanned=res['scanned'], added=[c['id'] for c in added])
            return dict(self.last)
