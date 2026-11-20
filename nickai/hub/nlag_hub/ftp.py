"""Everything the hub does to a console over its FTP server (GoldHEN FTP, port 2121 by default): install the package, stage model bundles, pin them.

All functions are blocking (run them in a thread). A console is a dict with at least {"host": ..., "ftp_port": 2121}. Nothing here touches anything outside the
app's own folders: /data/pkg (the package) and /data/nicklabs-native (models, models.json, trusted-extra.json)."""
import ftplib
import hashlib
import io
import json
import os
import pathlib

DATA = '/data/nicklabs-native'
PKG_DIR = '/data/pkg'
MODELS_JSON = DATA + '/models.json'
TRUST_EXTRA = DATA + '/trusted-extra.json'
SMALL_MAX = 8 << 20
BLOCK = 1 << 20
PKG_NAME = 'IV0000-NLNA00001_00-NICKLABSNATIVE01.pkg'


def connect(console, timeout=30):
    f = ftplib.FTP()
    f.connect(console['host'], int(console.get('ftp_port', 2121)), timeout=timeout)
    f.login()
    f.voidcmd('TYPE I')
    return f


def probe(console, timeout=5):
    """(ok, detail) - can we log in to the console's FTP server?"""
    try:
        f = connect(console, timeout)
        f.quit()
        return True, 'FTP server answered'
    except (OSError, ftplib.Error) as exc:
        return False, f'{type(exc).__name__}: {exc}'


def remote_size(f, path):
    try:
        return f.size(path)
    except ftplib.error_perm:
        return None


def ensure_dir(f, path):
    cur = ''
    for part in [p for p in path.split('/') if p]:
        cur += '/' + part
        try:
            f.mkd(cur)
        except ftplib.error_perm:
            pass   # exists (or cannot be created: the next operation will say so)


def download_bytes(f, path):
    b = bytearray()
    f.retrbinary('RETR ' + path, b.extend)
    return bytes(b)


def upload(f, local, remote, progress=None, cancelled=lambda: False, force=False):
    """Resumable by size (a partial file is continued, a complete one skipped). `force` replaces whatever is there (use it where a different file can have the same size)."""
    size = os.path.getsize(local)
    if force and remote_size(f, remote) is not None:
        try:
            f.delete(remote)
        except ftplib.error_perm:
            pass
    have = remote_size(f, remote)
    if have == size:
        if progress:
            progress(size, size)
        return
    start = have if have and have < size else 0
    with open(local, 'rb') as fh:
        fh.seek(start)
        sent = [start]

        def cb(chunk):
            if cancelled():
                raise InterruptedError('cancelled')
            sent[0] += len(chunk)
            if progress:
                progress(sent[0], size)
        f.storbinary('STOR ' + remote, fh, BLOCK, callback=cb, rest=start if start else None)
    got = remote_size(f, remote)
    if got != size:
        raise IOError(f'{remote}: size after upload is {got}, expected {size}')


def chmod(f, path, mode='644'):
    try:
        f.sendcmd(f'SITE CHMOD {mode} {path}')
    except ftplib.error_perm:
        pass


def put_json(f, path, obj):
    f.storbinary('STOR ' + path, io.BytesIO(json.dumps(obj, indent=1).encode() + b'\n'))
    chmod(f, path)


def get_json(f, path, default):
    try:
        return json.loads(download_bytes(f, path))
    except (ftplib.error_perm, ValueError):
        return default


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for b in iter(lambda: fh.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


# ----------------------------------------------------------------------------------------------------------------------------- package
def push_pkg(console, pkg_path, progress=None, cancelled=lambda: False):
    """Upload the installable package to /data/pkg and verify it byte for byte by SHA-256 over FTP... (size + hash of what the console now holds is
    not available over FTP, so the size is checked and the hash is reported for the user to compare in the console's package installer)."""
    pkg = pathlib.Path(pkg_path)
    f = connect(console)
    try:
        ensure_dir(f, PKG_DIR)
        dest = f'{PKG_DIR}/{PKG_NAME}'
        upload(f, pkg, dest, progress, cancelled, force=True)     # always replace: a new version of the package has the same size as the old one
        chmod(f, dest)
        local = sha256_file(pkg)
        remote = hashlib.sha256(download_bytes(f, dest)).hexdigest()    # read it back: what the console holds must be byte for byte what we sent
        if remote != local:
            raise IOError('the package on the console differs from the one sent (transfer error): try again')
        return {'remote': dest, 'bytes': pkg.stat().st_size, 'sha256': local, 'verified': True}
    finally:
        try:
            f.quit()
        except (OSError, ftplib.Error):
            pass


# ------------------------------------------------------------------------------------------------------------------------------ models
def remote_dir_for(name):
    d = f'{DATA}/models/{name}'
    if len(d) >= 80:
        raise ValueError('the model folder path must be shorter than 80 characters (console limit): use a shorter model name')
    return d


def stage_bundle(console, bundle_dir, name, trust=None, progress=None, cancelled=lambda: False, log=lambda m: None):
    """Upload a bundle folder, list it in models.json and (when `trust` is given) pin its hashes in trusted-extra.json. Safe to re-run."""
    bundle = pathlib.Path(bundle_dir)
    remote = remote_dir_for(name)
    files = sorted((p for p in bundle.iterdir() if p.is_file()), key=lambda p: (p.stat().st_size > SMALL_MAX, p.name))
    total = sum(p.stat().st_size for p in files)
    done_before = [0]
    f = connect(console)
    try:
        ensure_dir(f, remote)
        for p in files:
            def prog(sent, size, base=done_before[0]):
                if progress:
                    progress(base + sent, total)
            log(f'uploading {p.name}')
            upload(f, p, f'{remote}/{p.name}', prog, cancelled)
            chmod(f, f'{remote}/{p.name}')
            if p.stat().st_size <= SMALL_MAX and download_bytes(f, f'{remote}/{p.name}') != p.read_bytes():
                raise IOError(f'{p.name}: read-back differs from the local file')
            done_before[0] += p.stat().st_size
        cur = get_json(f, MODELS_JSON, {})
        bundles = [b for b in (cur.get('bundles') if isinstance(cur, dict) else []) or [] if isinstance(b, str)]
        if remote not in bundles:
            bundles.append(remote)
        put_json(f, MODELS_JSON, {'bundles': bundles})
        if trust:
            sets = get_json(f, TRUST_EXTRA, [])
            sets = [sets] if isinstance(sets, dict) else [s for s in sets if isinstance(s, dict)]
            if trust not in sets:
                sets.append(trust)
            put_json(f, TRUST_EXTRA, sets)
        return {'remote': remote, 'bundles': bundles}
    finally:
        try:
            f.quit()
        except (OSError, ftplib.Error):
            pass


def unstage_bundle(console, name):
    """Remove a bundle from models.json and delete its files (the pin stays harmlessly in trusted-extra.json)."""
    remote = remote_dir_for(name)
    f = connect(console)
    try:
        cur = get_json(f, MODELS_JSON, {})
        bundles = [b for b in (cur.get('bundles') if isinstance(cur, dict) else []) or [] if isinstance(b, str) and b != remote]
        put_json(f, MODELS_JSON, {'bundles': bundles})
        try:
            for entry in f.nlst(remote):
                try:
                    f.delete(entry if '/' in entry else f'{remote}/{entry}')
                except ftplib.error_perm:
                    pass
            f.rmd(remote)
        except ftplib.error_perm:
            pass
        return bundles
    finally:
        try:
            f.quit()
        except (OSError, ftplib.Error):
            pass


def free_space(console):
    """Not available over this FTP server; kept as an explicit placeholder so callers do not guess."""
    return None
