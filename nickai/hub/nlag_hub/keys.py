"""API keys for the tools that talk to the hub (OpenAI CLI/SDK, Claude CLI, Ollama clients, scripts).

On the trusted network no key is needed. Keys are optional and useful for two things: telling tools apart, and binding a key to ONE agent: every request made
with an agent key is attributed to that agent (the agent name sent by the client is ignored), so a campaign character cannot speak as another character.
Only the SHA-256 of a key is stored; the key itself is shown once, when it is created."""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

from . import core

PREFIX = 'nlag-'


class KeyStore:
    def __init__(self, path=None):
        self.path = path or (core.DATA / 'keys.json')
        self.lock = threading.RLock()
        self.items = []
        try:
            data = json.loads(open(self.path).read())
            self.items = [k for k in data if isinstance(k, dict) and 'hash' in k] if isinstance(data, list) else []
        except (OSError, ValueError):
            pass

    def _save(self):
        core.atomic_write(self.path, json.dumps(self.items, indent=1) + '\n')

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, name, agent=''):
        name = re.sub(r'[^\w .@:-]', '', str(name or '').strip())[:60] or 'key'
        agent = re.sub(r'[^A-Za-z0-9._:@-]', '', str(agent or ''))[:64]
        token = PREFIX + secrets.token_urlsafe(30)
        rec = {'id': secrets.token_hex(4), 'name': name, 'agent': agent, 'created': int(time.time()), 'last_used': 0, 'prefix': token[:10] + '…', 'hash': self._hash(token)}
        with self.lock:
            if len(self.items) >= 500:
                raise ValueError('too many keys: revoke some first')
            self.items.append(rec)
            self._save()
        return token, self.public(rec)

    def authenticate(self, token):
        """The key record for a presented token (constant-time comparison over hashes), else None."""
        if not token or not token.startswith(PREFIX):
            return None
        h = self._hash(token)
        with self.lock:
            for k in self.items:
                if hmac.compare_digest(k['hash'], h):
                    if time.time() - k.get('last_used', 0) > 60:   # do not write the file on every request
                        k['last_used'] = int(time.time())
                        self._save()
                    return k
        return None

    def revoke(self, key_id):
        with self.lock:
            n = len(self.items)
            self.items = [k for k in self.items if k['id'] != key_id]
            if len(self.items) == n:
                raise KeyError('unknown key')
            self._save()

    @staticmethod
    def public(k):
        return {x: k[x] for x in ('id', 'name', 'agent', 'created', 'last_used', 'prefix')}

    def list(self):
        with self.lock:
            return [self.public(k) for k in self.items]
