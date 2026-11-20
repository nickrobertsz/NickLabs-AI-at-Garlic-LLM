"""Linux process resource monitor. Base proposed by the Slim PS4 worker; per-field error isolation added by Claude."""
import os

def _field(content, key):
    for line in (content or "").splitlines():
        if line.startswith(key):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None

def proc_stats(pid):
    out = {"fds": None, "threads": None, "rss_kb": None}
    try:
        out["fds"] = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/status") as f:
            content = f.read()
    except OSError:
        return out
    out["threads"] = _field(content, "Threads:")
    out["rss_kb"] = _field(content, "VmRSS:")
    return out

def leaked(before, after, fd_slack=4, thread_slack=2, rss_slack_kb=50000):
    slacks = {"fds": fd_slack, "threads": thread_slack, "rss_kb": rss_slack_kb}
    res = []
    for m, slack in slacks.items():
        b, a = before.get(m), after.get(m)
        if b is None or a is None:
            continue
        if a - b > slack:
            res.append(f"{m} grew {b} -> {a} (+{a - b} > {slack})")
    return res
