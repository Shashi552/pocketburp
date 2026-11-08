#!/usr/bin/env python3
"""
PocketBurp -- repeater.py
Takes an edited raw HTTP request from the Repeater tab, sends it once to the
target, and returns the full raw response. Keeps a small in-memory history
of sends for the session so the GUI can restore earlier attempts.
"""

import threading
import time

import proxy

MAX_HISTORY = 40
MAX_KEEP = 400 * 1024      # cap stored response text per entry

_lock = threading.RLock()
_sends = []                # newest last
_next_id = [1]


def send(raw_text, host=None, port=None, scheme=None, timeout=30):
    """Send an arbitrary raw request. Returns a JSON-ready dict."""
    result = proxy.send_raw_request(
        raw_text,
        default_host=host,
        default_port=int(port) if port else None,
        default_scheme=scheme,
        timeout=timeout,
    )
    entry = {
        "id": _next_id[0],
        "ts": time.time(),
        "request": raw_text,
        "host": result.get("host") or host or "",
        "port": result.get("port") or port or 0,
        "scheme": result.get("scheme") or scheme or "http",
        "method": result.get("method") or "",
        "path": result.get("path") or "",
        "status": result.get("status") or 0,
        "status_line": result.get("status_line") or "",
        "headers": result.get("headers") or "",
        "response": (result.get("raw") or "")[:MAX_KEEP],
        "time_ms": result.get("time_ms") or 0,
        "ok": bool(result.get("ok")),
        "error": result.get("error") or "",
    }
    with _lock:
        _next_id[0] += 1
        _sends.append(entry)
        del _sends[:-MAX_HISTORY]
    out = dict(entry)
    out["body"] = result.get("raw") or ""
    out["length"] = len(result.get("body") or b"")
    out.pop("response", None)
    return out


def history():
    with _lock:
        out = []
        for e in reversed(_sends):
            d = dict(e)
            d.pop("response", None)      # keep the poll payload small
            out.append(d)
        return out


def get(entry_id):
    with _lock:
        for e in _sends:
            if e["id"] == int(entry_id):
                return dict(e)
    return None


def clear():
    with _lock:
        _sends[:] = []
    return True
