#!/usr/bin/env python3
"""
PocketBurp -- scope.py
Target scope list (one domain / IP / wildcard / CIDR per line) and the
"only intercept in-scope" switch. Out-of-scope traffic then passes through
totally untouched and unlogged.
"""

import ipaddress
import json
import os
import re
import threading

BASE = os.path.dirname(os.path.abspath(__file__))
SCOPE_FILE = os.path.join(BASE, "scope.txt")
CONFIG_FILE = os.path.join(BASE, "config.json")

_lock = threading.RLock()
_cache = {"mtime": None, "entries": [], "raw": "", "enforced": False}


def _read(path, default=""):
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return default


def _load_config():
    try:
        with open(CONFIG_FILE, "r") as fh:
            cfg = json.load(fh)
            return {"enforced": bool(cfg.get("scope_enforced", False))}
    except (OSError, ValueError):
        return {"enforced": False}


def _save_config(enforced):
    try:
        with open(CONFIG_FILE, "w") as fh:
            json.dump({"scope_enforced": bool(enforced)}, fh)
    except OSError:
        pass


def _norm_entry(line):
    """Accept 'example.com', '*.example.com', '10.0.0.0/8',
    'https://example.com:8443/path' -- return just the host/network part."""
    line = line.strip()
    if not line or line.startswith("#"):
        return ""
    if "://" in line:
        line = line.split("://", 1)[1]
    # keep CIDR notation intact
    if re.match(r"^[0-9a-fA-F.:]+/\d{1,3}$", line):
        return line.lower()
    line = line.split("/", 1)[0]          # drop any path
    if line.startswith("["):              # bracketed IPv6[:port]
        line = line[1:].split("]", 1)[0]
    elif line.count(":") == 1:            # host:port
        line = line.split(":", 1)[0]
    return line.lower().strip(".")


def _refresh():
    with _lock:
        try:
            mtime = os.path.getmtime(SCOPE_FILE)
        except OSError:
            mtime = 0
        cfg = _load_config()
        if _cache["mtime"] != mtime or _cache["enforced"] != cfg["enforced"]:
            raw = _read(SCOPE_FILE)
            entries = [e for e in (_norm_entry(l) for l in raw.splitlines()) if e]
            _cache.update(mtime=mtime, entries=entries, raw=raw,
                          enforced=cfg["enforced"])
        return _cache


def get():
    state = _refresh()
    return {
        "raw": state["raw"],
        "entries": list(state["entries"]),
        "enforced": bool(state["enforced"]),
    }


def save(raw, enforced=None):
    with _lock:
        try:
            with open(SCOPE_FILE, "w") as fh:
                fh.write(raw or "")
        except OSError:
            pass
        if enforced is not None:
            _save_config(enforced)
        _cache["mtime"] = None
    return get()


def _entry_matches(host, entry):
    host = host.lower().strip(".")
    if entry.startswith("*."):
        suffix = entry[2:]
        return host == suffix or host.endswith("." + suffix)
    try:
        if "/" in entry:
            return ipaddress.ip_address(host) in ipaddress.ip_network(entry, strict=False)
        ip = ipaddress.ip_address(host)
        try:
            return ip == ipaddress.ip_address(entry)
        except ValueError:
            return False
    except ValueError:
        pass
    return host == entry


def is_in_scope(host):
    """Empty scope list means everything is in scope (Burp-like default)."""
    state = _refresh()
    entries = state["entries"]
    if not entries:
        return True
    if not host:
        return False
    host = host.lower().strip(".")
    return any(_entry_matches(host, e) for e in entries)


def enforced():
    return bool(_refresh()["enforced"])
