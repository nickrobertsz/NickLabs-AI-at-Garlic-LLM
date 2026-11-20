#!/usr/bin/env python3
"""Load/chaos driver for a native node. soak.py HOST PORT [--seconds N] [--workers K] [--model NAME]
(The Fat PS4 worker's draft was truncated and wrong; rewritten by Claude.) Exit 1 on any unexpected outcome."""
import argparse, http.client, json, random, sys, threading, time
JH = {"Content-Type": "application/json"}

def chat_body(m, stream, content="hello", **opts):
    return json.dumps({"model": m, "stream": stream, "messages": [{"role": "user", "content": content}], "options": {"num_predict": 8, **opts}})

def act(name, host, port, m, rnd):
    c = http.client.HTTPConnection(host, port, timeout=60)
    try:
        if name == "chat":
            c.request("POST", "/api/chat", chat_body(m, False), JH); r = c.getresponse(); raw = r.read()
            return r.status == 503 or (r.status == 200 and json.loads(raw).get("done") is True), f"status {r.status}"
        if name == "chat_stream":
            c.request("POST", "/api/chat", chat_body(m, True), JH); r = c.getresponse(); raw = r.read()
            if r.status == 503: return True, ""
            lines = [l for l in raw.decode().split("\n") if l.strip()]
            return r.status == 200 and json.loads(lines[-1]).get("done") is True, f"status {r.status} lines {len(lines)}"
        if name == "abort":
            c.request("POST", "/api/chat", chat_body(m, True, "say long", num_predict=-1), JH); r = c.getresponse()
            if r.status == 200: r.fp.readline()
            ok = r.status in (200, 503); r.close(); return ok, f"status {r.status}"   # connection closed abruptly (finally below)
        if name == "openai_stream":
            c.request("POST", "/v1/chat/completions", json.dumps({"model": m, "stream": True, "max_tokens": 8, "messages": [{"role": "user", "content": "hello"}]}), JH)
            r = c.getresponse(); raw = r.read()
            return r.status == 503 or (r.status == 200 and raw.rstrip().endswith(b"data: [DONE]")), f"status {r.status}"
        if name == "anthropic":
            c.request("POST", "/v1/messages", json.dumps({"model": m, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}), JH); r = c.getresponse(); raw = r.read()
            return r.status == 503 or (r.status == 200 and json.loads(raw).get("type") == "message"), f"status {r.status}"
        if name == "badjson":
            c.request("POST", "/api/chat", b"{not json", JH); r = c.getresponse(); r.read(); return r.status == 400, f"status {r.status}"
        if name == "tags":
            c.request("GET", "/api/tags"); r = c.getresponse(); return r.status == 200 and "models" in json.loads(r.read()), f"status {r.status}"
        raise ValueError(name)
    finally:
        c.close()

ACTIONS = ["chat", "chat_stream", "abort", "openai_stream", "anthropic", "badjson", "tags"]

def soak(host, port, seconds, workers, model):
    counts = {a: {"ok": 0, "fail": 0} for a in ACTIONS}; problems = []; lock = threading.Lock(); end = time.monotonic() + seconds
    def worker(i):
        rnd = random.Random(i)
        while time.monotonic() < end:
            a = rnd.choice(ACTIONS)
            try: ok, detail = act(a, host, port, model, rnd)
            except Exception as e: ok, detail = False, f"{type(e).__name__}: {e}"
            with lock:
                counts[a]["ok" if ok else "fail"] += 1
                if not ok and len(problems) < 20: problems.append(f"{a}: {detail}")
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return {"seconds": seconds, "workers": workers, "counts": counts, "problems": problems}

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("host"); ap.add_argument("port", type=int); ap.add_argument("--seconds", type=float, default=30); ap.add_argument("--workers", type=int, default=8); ap.add_argument("--model", default="fixture-a")
    a = ap.parse_args(); rep = soak(a.host, a.port, a.seconds, a.workers, a.model); print(json.dumps(rep, indent=2))
    sys.exit(1 if any(v["fail"] for v in rep["counts"].values()) else 0)
