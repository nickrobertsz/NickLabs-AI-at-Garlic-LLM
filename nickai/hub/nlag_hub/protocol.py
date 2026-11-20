"""Pure helpers for the Ollama / OpenAI / Anthropic wire formats the consoles speak (no I/O)."""
import json
import re

INFERENCE = {('POST', '/api/chat'), ('POST', '/api/generate'), ('POST', '/api/show'), ('POST', '/v1/chat/completions'), ('POST', '/v1/completions'),
             ('POST', '/v1/messages'), ('POST', '/v1/messages/count_tokens'), ('POST', '/api/embed'), ('POST', '/api/embeddings'), ('POST', '/v1/embeddings')}
# a stream chunk that carries generated text (not the role / start header): the moment the first token reached the client
FIRST_TEXT = re.compile(rb'"(?:content|text|thinking|reasoning_content|response)"\s*:\s*"[^"]')
HOP = {'connection', 'keep-alive', 'transfer-encoding', 'content-length', 'host', 'upgrade', 'te', 'trailer', 'proxy-authorization'}
PASS_HEADERS = ('content-type', 'retry-after', 'cache-control')


def split_model(name):
    """'qwen3.5-4b-garlic@ps4-2' -> ('qwen3.5-4b-garlic', 'ps4-2'); no suffix -> (name, None)."""
    if not isinstance(name, str):
        return None, None
    base, sep, console = name.rpartition('@')
    if sep and base and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,30}', console):
        return base, console
    return name, None


def agent_of(headers, body):
    """Who is asking: X-Agent-Id, else OpenAI `user`, else Anthropic metadata.user_id, else Ollama options.agent. Always a short safe token."""
    raw = headers.get('X-Agent-Id') or ''
    if not raw and isinstance(body, dict):
        raw = body.get('user') if isinstance(body.get('user'), str) else ''
        if not raw and isinstance(body.get('metadata'), dict) and isinstance(body['metadata'].get('user_id'), str):
            raw = body['metadata']['user_id']
        if not raw and isinstance(body.get('options'), dict) and isinstance(body['options'].get('agent'), str):
            raw = body['options']['agent']
    return re.sub(r'[^A-Za-z0-9._:@-]', '', raw)[:64]


def rewrite_model_line(line: bytes, public: str, bare: str) -> bytes:
    """Rewrite the model name in one NDJSON line or SSE 'data: ' line (also Anthropic message_start); never raises."""
    prefix, payload = b'', line
    if line.startswith(b'data: '):
        prefix, payload = b'data: ', line[6:]
    try:
        obj = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return line
    if not isinstance(obj, dict):
        return line
    changed = False
    if obj.get('model') == bare:
        obj['model'] = public
        changed = True
    msg = obj.get('message')
    if isinstance(msg, dict) and msg.get('model') == bare:
        msg['model'] = public
        changed = True
    if not changed:
        return line
    return prefix + json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def merge_tags(per_console, with_consoles=False):
    """Ollama /api/tags across consoles: one entry per distinct model name (the first console's details), annotated with where it is installed/loaded.
    per_console = [(console_id, tags_json | None, loaded_name)]. With `with_consoles` also one 'name@console' entry per installation."""
    out, seen = [], {}
    for cid, tags, loaded in per_console:
        for m in (tags or {}).get('models', []) if isinstance(tags, dict) else []:
            if not isinstance(m, dict) or not isinstance(m.get('name'), str):
                continue
            name = m['name']
            if name not in seen:
                item = dict(m)
                item['nicklabs'] = {'consoles': [], 'loaded_on': []}
                seen[name] = item
                out.append(item)
            seen[name]['nicklabs']['consoles'].append(cid)
            if loaded == name:
                seen[name]['nicklabs']['loaded_on'].append(cid)
            if with_consoles:
                e = dict(m)
                e['name'] = e['model'] = f'{name}@{cid}'
                e['nicklabs'] = {'consoles': [cid], 'loaded_on': [cid] if loaded == name else []}
                out.append(e)
    return {'models': out}


def merge_openai_models(tags):
    return {'object': 'list', 'data': [{'id': m['name'], 'object': 'model', 'owned_by': 'nicklabs-ai-on-garlic'} for m in tags['models'] if '@' not in m['name']]}
