"""The model library (bundles built on the user's computer) and the background jobs that fill it and push it to consoles."""
import json
import pathlib
import shutil
import threading
import time
import traceback
import uuid

from . import core, ftp, sourcing
from .convert import qwen35, sources

LIB = lambda: core.DATA / 'library'            # noqa: E731
CACHE = lambda: core.DATA / 'cache'            # noqa: E731
WORK = lambda: core.DATA / 'work'              # noqa: E731


class Job:
    def __init__(self, kind, title):
        self.id = uuid.uuid4().hex[:10]
        self.kind, self.title = kind, title
        self.state, self.progress, self.message = 'queued', 0.0, ''
        self.created, self.finished = time.time(), None
        self.lines = []
        self.result = None
        self.cancel = threading.Event()

    def to_json(self, full=False):
        d = {'id': self.id, 'kind': self.kind, 'title': self.title, 'state': self.state, 'progress': round(self.progress, 4), 'message': self.message,
             'created': self.created, 'finished': self.finished, 'result': self.result}
        if full:
            d['log'] = self.lines[-300:]
        return d


class Jobs:
    def __init__(self, log=lambda *a, **k: None):
        self.jobs = {}
        self.lock = threading.Lock()
        self.log = log
        self.path = core.DATA / 'jobs.json'
        try:
            for j in json.loads(self.path.read_text()):
                job = Job(j['kind'], j['title'])
                job.__dict__.update({k: j[k] for k in ('id', 'state', 'progress', 'message', 'created', 'finished', 'result') if k in j})
                if job.state in ('queued', 'running'):
                    job.state, job.message = 'failed', 'interrupted by a restart: run it again (downloads resume)'
                self.jobs[job.id] = job
        except (OSError, ValueError, KeyError):
            pass

    def _persist(self):
        try:
            core.atomic_write(self.path, json.dumps([j.to_json() for j in list(self.jobs.values())[-200:]]))
        except OSError:
            pass

    def start(self, kind, title, fn):
        job = Job(kind, title)
        with self.lock:
            self.jobs[job.id] = job

        def run():
            job.state = 'running'
            self.log('jobs', f'{kind} started: {title}')

            def note(msg, frac=None):
                job.message = msg
                job.lines.append(f'{time.strftime("%H:%M:%S")} {msg}')
                if frac is not None:
                    job.progress = max(job.progress, min(1.0, frac))
            try:
                job.result = fn(job, note)
                job.state, job.progress, job.message = 'done', 1.0, job.message or 'done'
            except InterruptedError:
                job.state, job.message = 'cancelled', 'cancelled'
            except Exception as exc:  # noqa: BLE001 - report every failure to the user, with the cause
                job.state, job.message = 'failed', f'{type(exc).__name__}: {exc}'
                job.lines.append(traceback.format_exc()[-1500:])
                self.log('jobs', f'{kind} FAILED: {title}: {type(exc).__name__}: {exc}')
            job.finished = time.time()
            self.log('jobs', f'{kind} {job.state}: {title}')
            self._persist()
        threading.Thread(target=run, daemon=True, name=f'job-{job.id}').start()
        return job

    def list(self):
        return [j.to_json() for j in sorted(self.jobs.values(), key=lambda j: -j.created)][:60]

    def get(self, jid):
        return self.jobs.get(jid)


# ------------------------------------------------------------------------------------------------------------------------------ library
def library():
    out = []
    root = LIB()
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        try:
            meta = json.loads((d / 'meta.json').read_text())
            out.append(meta)
        except (OSError, ValueError):
            continue
    return out


def library_get(model_id):
    for m in library():
        if m['id'] == model_id:
            return m
    return None


def library_delete(model_id):
    m = library_get(model_id)
    if not m:
        raise sourcing.SourceError(f'{model_id!r} is not in the library')
    shutil.rmtree(LIB() / model_id, ignore_errors=True)


def unique_id(base):
    ids = {m['id'] for m in library()}
    if base not in ids:
        return base
    n = 2
    while f'{base}-{n}' in ids:
        n += 1
    return f'{base}-{n}'


def _safe_import_path(p):
    p = pathlib.Path(p).resolve()
    roots = [core.DATA.resolve(), pathlib.Path('/import')]
    if not any(p == r or r in p.parents for r in roots if r.exists()) and not any(p == r or r in p.parents for r in [x.resolve() for x in sourcing.ollama_dirs()]):
        raise sourcing.SourceError('local paths must be inside the data folder, /import or an Ollama store: put the model in the "import" folder next to the launcher')
    return p


def build_from_source(src, model_id, origin, note, cancelled, size_hint=''):
    """Convert an opened source into a library bundle. Returns the library meta."""
    cfg = src.cfg
    est = qwen35.estimate_bytes(cfg)
    if est > qwen35.GPU_WEIGHT_BUDGET:
        raise ValueError(f'this model needs about {est / 1e9:.1f} GB of weights, but a console holds about {qwen35.GPU_WEIGHT_BUDGET / 1e9:.1f} GB. '
                         'The engine keeps all weights in one console\'s GPU memory; splitting a model over several consoles is not implemented yet.')
    bad = qwen35.shape_problem(cfg)
    if bad:
        raise ValueError(bad[0].upper() + bad[1:])
    arch = 'qwen3.5-' + qwen35.size_label(cfg, size_hint or src.label())
    work = WORK() / model_id
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        note(f'converting {origin} ({arch}, ~{est / 1e9:.1f} GB) - this takes a few minutes', 0.02)
        qwen35.convert(src, work, model_id, progress=lambda m, f: note(m, f), cancelled=cancelled)
        out = LIB() / model_id
        built = qwen35.make_bundle(work, out, model_id, arch, core.ASSETS, display_name=model_id)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    meta = {'id': model_id, 'name': built['name'], 'arch': arch, 'origin': origin, 'bytes': built['weights_bytes'], 'created': int(time.time()), 'trust': built['trust'],
            'quantization': 'q4i', 'source_kind': src.kind}
    (out / 'meta.json').write_text(json.dumps(meta, indent=1))
    note(f'{model_id} is ready in the library ({built["weights_bytes"] / 1e9:.2f} GB)', 0.97)
    return meta


def import_job(spec, name=None, push_to=(), consoles=None):
    """Return a job function for Jobs.start. spec: 'ollama-local:qwen3.5:4b' | 'ollama:qwen3.5:4b' | 'hf:Qwen/Qwen3.5-4B' | 'path:/import/model.gguf'."""
    kind, _, ref = spec.partition(':')
    if kind not in ('ollama-local', 'ollama', 'hf', 'path') or not ref:
        raise sourcing.SourceError('source must be ollama-local:<name:tag>, ollama:<name:tag>, hf:<owner/repo> or path:<file or folder>')

    def run(job, note):
        cancelled = job.cancel.is_set
        prog = lambda g, t: note(f'downloading {g / 1e6:.0f} / {t / 1e6:.0f} MB', 0.02 + 0.5 * (g / max(t, 1)))   # noqa: E731
        if kind == 'ollama-local':
            m = sourcing.find_ollama_local(ref)
            if not m['supported']:
                raise sourcing.SourceError(m['reason'])
            src, origin, hint = sources.GGUFSource(m['blob']), f'Ollama (your computer): {ref}', ref
        elif kind == 'ollama':
            blob = sourcing.pull_ollama(ref, CACHE() / 'ollama', prog, cancelled, lambda s: note(s))
            src, origin, hint = sources.GGUFSource(blob), f'Ollama registry: {ref}', ref
        elif kind == 'hf':
            d = sourcing.pull_hf(ref, CACHE() / 'hf' / sourcing.slug(ref), prog, cancelled, lambda s: note(s))
            src, origin, hint = sources.HFSource(d), f'HuggingFace: {ref}', ref
        else:
            p = _safe_import_path(ref)
            src, origin, hint = sources.open_source(p), f'file: {p.name}', p.name
        arch_hint = qwen35.size_label(src.cfg, hint)
        base = name or sourcing.slug(f'qwen3.5-{arch_hint}-{src.kind}')
        if not sourcing.NAME_RE.match(base):
            raise sourcing.SourceError('model name: letters, digits, . _ -')
        mid = unique_id(base) if not name else name
        if name and library_get(name):
            raise sourcing.SourceError(f'{name!r} already exists in the library: choose another name or delete it first')
        if len(ftp.DATA + '/models/' + mid) >= 80:
            raise sourcing.SourceError('model name too long for the console (path limit): use at most ~40 characters')
        meta = build_from_source(src, mid, origin, note, cancelled, hint)
        results = {}
        for cid in push_to:
            results[cid] = push(meta['id'], (consoles or {}).get(cid), note, cancelled)
        return {'model': meta, 'pushed': results}
    return run


def push(model_id, console, note=lambda *a: None, cancelled=lambda: False):
    """Upload a library bundle to one console (resumable) and pin it."""
    m = library_get(model_id)
    if not m:
        raise sourcing.SourceError(f'{model_id!r} is not in the library')
    if not console:
        raise sourcing.SourceError('unknown console')
    note(f'sending {model_id} to {console["name"]} ({console["host"]})', 0.98 if False else None)
    t0 = time.time()

    def prog(done, total):
        note(f'sending to {console["name"]}: {done / 1e6:.0f} / {total / 1e6:.0f} MB ({done / max(time.time() - t0, 1) / 1e6:.1f} MB/s)', None)
    r = ftp.stage_bundle(console, LIB() / model_id, model_id, m['trust'], prog, cancelled, lambda s: note(s))
    note(f'{model_id} is on {console["name"]}: load it from the dashboard or `nlag load {model_id} {console["id"]}`', 1.0)
    return r


def push_job(model_id, console):
    def run(job, note):
        return push(model_id, console, note, job.cancel.is_set)
    return run


def install_pkg_job(console, pkg_path):
    def run(job, note):
        t0 = time.time()
        r = ftp.push_pkg(console, pkg_path, lambda d, t: note(f'sending the package to {console["name"]}: {d / 1e6:.0f} / {t / 1e6:.0f} MB', d / max(t, 1)), job.cancel.is_set)
        note(f'package is on {console["name"]} at {r["remote"]} (sha256 {r["sha256"][:12]}...) in {time.time() - t0:.0f}s: install it with Package Installer', 1.0)
        return r
    return run
