"""Continuous, persistent log capture from every registered console, kept on the user's computer (inside the hub's data volume).

Per console, under <data>/logs/<console-id>/:
  klog-YYYYMMDD.log    EVERY line of the console's KLOG stream (port 3232: kernel + system + app output), prefixed with the receive time (UTC).
                       The KLOG server replays its ring buffer to the FIRST reader only and the read is destructive: the hub must be the ONLY reader.
  events-YYYYMMDD.log  the interesting subset (our app, app focus/kill/suspend, coredump, crash reporter, GPU, dmem) plus capture markers
  boot.log, native.log local cumulative copies of /data/nicklabs-native/{boot,native}.log, fetched incrementally over FTP (SIZE + REST), so they survive
                       the console overwriting or truncating them (a truncation starts a clearly marked new segment)
  coredumps/           any new file appearing in /data/sce_coredumps (downloaded once, size-capped)
  api.jsonl            the app's API up/down transitions and status snapshots whenever the model/engine state changes
and <data>/logs/hub/hub-YYYYMMDD.log: what the hub itself did (consoles online/offline, loads, jobs, errors).
Read-only against the consoles except `SITE CHMOD 644` on the app's own boot/native logs when FTP denies the read (they are 0600 owned by the app uid)."""
import datetime, ftplib, json, os, pathlib, re, socket, threading, time, urllib.request

FTP_PORT, KLOG_PORT, API_PORT = 2121, 3232, 11434
REMOTE_DIR = '/data/nicklabs-native'
REMOTE_FILES = ('boot.log', 'native.log')
COREDUMP_DIR = '/data/sce_coredumps'
COREDUMP_MAX = 2 << 30
FILES_EVERY, API_EVERY, HEARTBEAT_EVERY = 15.0, 10.0, 5.0
FIRST_FETCH_CAP = 16 << 20            # a first sight of a huge remote log starts at its last 16 MiB (marked) instead of stalling on a slow link
RELEVANT = re.compile(r'NLNA00001|NLN01|NickLabs|AppFocusChanged|KillApp|BlockingKill|Kill App|Kill for LoadExec|dmem_start_app_process|Kill process|Suspend App|Resume App|NOTE_EXIT|'
                      r'CRASHREPORT|coredump|SIGABRT|SIGSEGV|SIGBUS|panic|GPU|GNM|gfx|dmem|vmspace|out of memory|Garlic|Onion', re.I)
STATE_KEYS = ('model', 'loaded', 'loaded_model', 'state', 'pending_swap', 'restarts_since_ok', 'last_error', 'build')
BACKEND_KEYS = ('engine', 'engine_ctx_limit', 'gpu_fenced', 'restarting', 'gpu_reserved_bytes', 'mtp', 'model', 'loaded')


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def stamp():
    return now().strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


class LineSplitter:
    """Assemble text lines from a byte stream. Decodes only COMPLETE lines (so a split multi-byte character survives); bounded memory."""
    def __init__(self, max_line=65536):
        self.buf = bytearray(); self.max = max_line

    @staticmethod
    def _dec(b):
        if b.endswith(b'\r'): b = b[:-1]
        return bytes(b).decode('utf-8', 'replace')

    def feed(self, data):
        out = []
        if not data: return out
        self.buf += data
        while True:
            i = self.buf.find(b'\n')
            if i < 0: break
            out.append(self._dec(self.buf[:i])); del self.buf[:i + 1]
        while len(self.buf) >= self.max:                 # no newline in max_line bytes: emit what we have, stay bounded
            out.append(self._dec(self.buf[:self.max])); del self.buf[:self.max]
        return out

    def flush(self):
        if not self.buf: return []
        line = self._dec(self.buf); self.buf.clear(); return [line]


class DailyFile:
    """Append-only text file named <prefix>-YYYYMMDD.log (UTC), rotating at midnight, flushed on every write."""
    def __init__(self, directory, prefix):
        self.dir = pathlib.Path(directory); self.prefix = prefix; self.day = None; self.f = None; self.lock = threading.Lock()

    def write(self, line):
        with self.lock:
            day = now().strftime('%Y%m%d')
            if day != self.day or self.f is None:
                if self.f: self.f.close()
                self.dir.mkdir(parents=True, exist_ok=True); self.f = open(self.dir / f'{self.prefix}-{day}.log', 'a', encoding='utf-8', errors='replace'); self.day = day
            self.f.write(line.replace('\x00', '') + '\n'); self.f.flush()


class Console:
    def __init__(self, name, host, base, ports=None):
        self.name, self.host = name, host; self.dir = pathlib.Path(base) / name; self.dir.mkdir(parents=True, exist_ok=True)
        p = ports or {}; self.ftp_port = p.get('ftp', FTP_PORT); self.klog_port = p.get('klog', KLOG_PORT); self.api_port = p.get('api', API_PORT)
        self.klog = DailyFile(self.dir, 'klog'); self.events = DailyFile(self.dir, 'events'); self.stop = threading.Event()
        self.st = {'klog_connected': False, 'klog_bytes': 0, 'klog_lines': 0, 'klog_last_at': None, 'klog_error': None, 'ftp_ok': None, 'ftp_error': None,
                   'files': {}, 'coredumps': [], 'api_up': None, 'api_since': None, 'started': stamp()}
        self.lock = threading.Lock()

    def close(self):
        for d in (self.klog, self.events):
            with d.lock:
                if d.f: d.f.close(); d.f = None

    def mark(self, text, klog=True):
        line = f'{stamp()} #### {text}'; self.events.write(line)
        if klog: self.klog.write(line)

    def set(self, **kw):
        with self.lock: self.st.update(kw)

    # ---- KLOG ------------------------------------------------------------------------------------------------------------------------
    def klog_loop(self):
        backoff = 1.0; last_err = None; quick = []
        while not self.stop.is_set():
            try:
                s = socket.create_connection((self.host, self.klog_port), timeout=5)
            except OSError as e:
                err = f'{type(e).__name__}: {e}'
                if err != last_err: self.mark(f'klog connect failed: {err}', klog=False); last_err = err
                self.set(klog_connected=False, klog_error=err); self.stop.wait(backoff); backoff = min(backoff * 2, 15); continue
            backoff = 1.0; last_err = None
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                for opt, v in (('TCP_KEEPIDLE', 20), ('TCP_KEEPINTVL', 5), ('TCP_KEEPCNT', 3)):
                    if hasattr(socket, opt): s.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), v)
            except OSError: pass
            s.settimeout(2.0); self.mark('klog connected'); self.set(klog_connected=True, klog_error=None); split = LineSplitter(); why = 'eof'
            try:
                while not self.stop.is_set():
                    try: data = s.recv(65536)
                    except socket.timeout: continue
                    if not data: break
                    lines = split.feed(data); ts = stamp()
                    for ln in lines:
                        self.klog.write(f'{ts} {ln}')
                        if RELEVANT.search(ln): self.events.write(f'{ts} {ln}')
                    with self.lock: self.st['klog_bytes'] += len(data); self.st['klog_lines'] += len(lines); self.st['klog_last_at'] = ts
            except OSError as e: why = f'{type(e).__name__}: {e}'
            finally:
                try: s.close()
                except OSError: pass
            for ln in split.flush(): self.klog.write(f'{stamp()} {ln}')
            self.mark(f'klog disconnected ({why})'); self.set(klog_connected=False, klog_error=why)
            quick.append(time.time()); quick[:] = [t for t in quick if time.time() - t < 90]
            if len(quick) >= 3:   # the console's log server serves ONE reader and drops the previous one when another connects: two programs fight
                msg = 'another program is reading this console\'s log port (3232): the console allows one reader. Close it (other log tools, a second hub) so this one keeps a complete record.'
                self.mark(msg, klog=False); self.set(klog_error=msg); self.stop.wait(45)

    # ---- files over FTP --------------------------------------------------------------------------------------------------------------
    def _fetch(self, ftp, path, offset):
        data = bytearray()
        try: ftp.retrbinary('RETR ' + path, data.extend, rest=offset if offset else None)
        except ftplib.error_perm as e:
            if not str(e).startswith('550') or not path.startswith(REMOTE_DIR): raise
            ftp.sendcmd('SITE CHMOD 644 ' + path); self.mark(f'ftp: SITE CHMOD 644 {path} (read was denied)', klog=False)
            data = bytearray(); ftp.retrbinary('RETR ' + path, data.extend, rest=offset if offset else None)
        return bytes(data)

    def sync_files(self, ftp):
        ftp.voidcmd('TYPE I')
        for name in REMOTE_FILES:
            path = f'{REMOTE_DIR}/{name}'; fs = self.st['files'].setdefault(name, {'offset': None, 'remote_size': None})
            try: size = ftp.size(path)
            except ftplib.error_perm: fs['remote_size'] = None; continue            # not created yet
            if size is None: continue
            local = self.dir / name; off = fs['offset']
            if off is None:
                off = local.stat().st_size if local.exists() else 0                  # resume after a daemon restart: the local copy is cumulative
                if size - off > FIRST_FETCH_CAP:
                    skipped = size - FIRST_FETCH_CAP; self.mark(f'{name}: remote is {size} bytes, starting at {skipped} (skipped {skipped - off})', klog=False); off = skipped
            if size < off:
                self.mark(f'{name}: remote shrank {off} -> {size}: starting a new local segment', klog=False)
                with open(local, 'ab') as f: f.write(f'\n#### {stamp()} remote file truncated (was {off} bytes), new segment follows\n'.encode())
                off = 0
            if size > off:
                chunk = self._fetch(ftp, path, off)
                if chunk:
                    with open(local, 'ab') as f: f.write(chunk)
                    off += len(chunk)
            fs['offset'] = off; fs['remote_size'] = size
        try: names = ftp.nlst(COREDUMP_DIR)
        except ftplib.error_perm: names = []
        except ftplib.error_temp: names = []
        known = set(self.st['coredumps'])
        for raw in names:
            n = raw.rsplit('/', 1)[-1]
            if not n or n in ('.', '..') or n in known: continue
            known.add(n); self.st['coredumps'] = sorted(known); cdir = self.dir / 'coredumps'; cdir.mkdir(exist_ok=True)
            try: size = ftp.size(f'{COREDUMP_DIR}/{n}')
            except ftplib.error_perm: size = None
            self.mark(f'COREDUMP appeared: {n} ({size} bytes)')
            if size is not None and size <= COREDUMP_MAX:
                data = bytearray(); ftp.retrbinary(f'RETR {COREDUMP_DIR}/{n}', data.extend); (cdir / n).write_bytes(bytes(data)); self.mark(f'coredump saved: {cdir / n}')

    def files_loop(self):
        last = None
        while not self.stop.is_set():
            try:
                ftp = ftplib.FTP(); ftp.connect(self.host, self.ftp_port, timeout=10); ftp.login()
                try: self.sync_files(ftp)
                finally:
                    try: ftp.close()
                    except Exception: pass
                if last is not None: self.mark('ftp ok again', klog=False)
                last = None; self.set(ftp_ok=True, ftp_error=None)
            except Exception as e:
                err = f'{type(e).__name__}: {str(e)[:120]}'
                if err != last: self.mark(f'ftp error: {err}', klog=False); last = err
                self.set(ftp_ok=False, ftp_error=err)
            self.stop.wait(FILES_EVERY)

    # ---- native API state ------------------------------------------------------------------------------------------------------------
    @staticmethod
    def signature(status):
        sig = {k: status.get(k) for k in STATE_KEYS if k in status}; be = status.get('backend') or {}
        sig['backend'] = {k: be.get(k) for k in BACKEND_KEYS if k in be}
        return json.dumps(sig, sort_keys=True)

    def api_loop(self):
        up = None; sig = None; path = self.dir / 'api.jsonl'
        def rec(**kw):
            with open(path, 'a') as f: f.write(json.dumps({'t': stamp(), **kw}) + '\n')
        while not self.stop.is_set():
            status = None
            try:
                with socket.create_connection((self.host, self.api_port), timeout=3): pass
                now_up = True
                try:
                    with urllib.request.urlopen(f'http://{self.host}:{self.api_port}/native/status', timeout=5) as r: status = json.loads(r.read())
                except Exception: status = None
            except OSError: now_up = False
            if now_up != up:
                self.mark(f'native api {"UP" if now_up else "DOWN"} on :{self.api_port}', klog=False); rec(event='api_up' if now_up else 'api_down', console=self.name)
                up = now_up; sig = None; self.set(api_up=now_up, api_since=stamp())
            if now_up and isinstance(status, dict):
                s = self.signature(status)
                if s != sig:
                    sig = s; rec(event='status', console=self.name, status={k: v for k, v in status.items() if k != 'config'}); self.mark('native status changed: ' + s[:300], klog=False)
            self.stop.wait(API_EVERY)

    def threads(self):
        return [threading.Thread(target=f, name=f'{self.name}-{f.__name__}', daemon=True) for f in (self.klog_loop, self.files_loop, self.api_loop)]


class Capture:
    """Runs one Console capture (three threads) per registered console and follows the registry; also writes the hub's own log and enforces retention."""

    def __init__(self, registry, settings, base):
        self.registry, self.settings, self.base = registry, settings, pathlib.Path(base)
        self.consoles = {}
        self.stop = threading.Event()
        self.hub = DailyFile(self.base / 'hub', 'hub')
        self.version = -1
        self.thread = None

    def log(self, source, message):
        self.hub.write(f'{stamp()} [{source}] {message}')

    def start(self):
        self.base.mkdir(parents=True, exist_ok=True)
        self.log('hub', 'capture started')
        self.thread = threading.Thread(target=self._loop, daemon=True, name='capture-manager')
        self.thread.start()

    def _loop(self):
        last_prune = 0.0
        while not self.stop.is_set():
            try:
                if self.version != self.registry.version:
                    self.sync()
                if time.time() - last_prune > 3600:
                    last_prune = time.time()
                    self.prune()
            except Exception as exc:  # noqa: BLE001 - logging must never take the hub down
                self.log('capture', f'error {type(exc).__name__}: {exc}')
            self.stop.wait(2)

    def sync(self):
        want = {c['id']: c for c in self.registry.list()}
        for cid in list(self.consoles):
            c = self.consoles[cid]
            w = want.get(cid)
            if not w or (w['host'], w['api_port'], w['ftp_port'], w['klog_port']) != (c.host, c.api_port, c.ftp_port, c.klog_port):
                c.stop.set()
                c.mark('capture stopped (console removed or its address changed)', klog=False)
                c.close()
                del self.consoles[cid]
        for cid, w in want.items():
            if cid not in self.consoles:
                c = Console(cid, w['host'], self.base, {'ftp': w['ftp_port'], 'klog': w['klog_port'], 'api': w['api_port']})
                c.mark('capture started', klog=False)
                for t in c.threads():
                    t.start()
                self.consoles[cid] = c
        self.version = self.registry.version

    def status(self):
        return {cid: dict(c.st) for cid, c in self.consoles.items()}

    def close(self):
        self.stop.set()
        for c in self.consoles.values():
            c.stop.set()
            c.close()

    def prune(self):
        """Delete day files older than the retention, then the oldest day files while the total is above the size cap."""
        days, cap = int(self.settings.values['log_retention_days']), int(self.settings.values['log_max_mb']) << 20
        cutoff = time.time() - days * 86400
        files = [p for p in self.base.rglob('*-????????.log') if p.is_file()]
        for p in files:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        files = sorted((p for p in self.base.rglob('*-????????.log') if p.is_file()), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files[:-3]:
            if total <= cap:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
