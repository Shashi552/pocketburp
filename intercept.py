#!/usr/bin/env python3
"""
PocketBurp -- intercept.py
The "held request" queue that powers the Intercept tab.

Proxy worker threads call add() and then block on wait() until the GUI
forwards, edits, or drops the request. Also supports a "pass-through"
mode where the parser tells the proxy to auto-forward, and auto-forward
of static asset requests so Safari does not hang on icons/CSS.
"""

import os
import re
import threading
import time

STATIC_EXT = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".mp4", ".webm", ".mp3",
    ".avif", ".bmp", ".otf",
)
# Hosts that are pure noise on iOS; never held even with intercept ON.
NOISE_HOSTS = (
    "captive.apple.com", "configuration.apple.com", "gsp-ssl.ls.apple.com",
    "time.apple.com", "time-ios.apple.com", "bag.itunes.apple.com",
)

ACTION_FORWARD = "forward"
ACTION_DROP = "drop"


class HeldRequest(object):
    def __init__(self, rid, meta, raw):
        self.id = rid
        self.meta = meta            # dict: method, host, port, scheme, path, tls, source
        self.raw = raw              # editable raw request text
        self.ts = time.time()
        self.action = None
        self.edited_raw = None
        self.event = threading.Event()


class InterceptQueue(object):
    def __init__(self):
        self._lock = threading.RLock()
        self._held = {}            # id -> HeldRequest
        self._order = []           # ids, oldest first
        self._next_id = 1
        self.enabled = True
        self.auto_skip_static = True
        self.stats = {
            "held_total": 0,
            "forwarded": 0,
            "dropped": 0,
            "edited": 0,
            "intercepted_requests": 0,
            "passed_through": 0,
        }

    # ------------------------------------------------------------- helpers
    def should_hold(self, meta):
        if not self.enabled:
            return False
        host = (meta.get("host") or "").lower()
        path = (meta.get("path") or "").lower()
        if host in NOISE_HOSTS:
            return False
        if self.auto_skip_static:
            clean_path = path.split("?", 1)[0]
            if clean_path.endswith(STATIC_EXT):
                return False
        return True

    def _raw_from_meta(self, meta):
        headers = meta.get("headers") or []
        lines = ["%s %s HTTP/1.1" % (meta.get("method", "GET"), meta.get("path", "/"))]
        lines.extend(headers)
        body = meta.get("body_preview") or ""
        return "\r\n".join(lines) + "\r\n\r\n" + body

    # ---------------------------------------------------------------- core
    def add(self, meta, timeout=300):
        """Hold a request. Returns ('forward', raw_text) or ('drop', None)."""
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            item = HeldRequest(rid, meta, self._raw_from_meta(meta))
            self._held[rid] = item
            self._order.append(rid)
            self.stats["held_total"] += 1
        if not item.event.wait(timeout):
            self.resolve(rid, ACTION_FORWARD, None, reason="timeout")
        return (item.action or ACTION_FORWARD), (item.edited_raw or item.raw)

    def resolve(self, rid, action, edited_raw=None, reason=""):
        with self._lock:
            item = self._held.get(rid)
            if item is None or item.event.is_set():
                return False
            item.action = action
            item.edited_raw = edited_raw
            if action == ACTION_DROP:
                self.stats["dropped"] += 1
            else:
                self.stats["forwarded"] += 1
                if edited_raw and edited_raw != item.raw:
                    self.stats["edited"] += 1
            item.event.set()
        return True

    def forward_all(self):
        n = 0
        for rid in self.ids():
            if self.resolve(rid, ACTION_FORWARD):
                n += 1
        return n

    def drop_all(self):
        n = 0
        for rid in self.ids():
            if self.resolve(rid, ACTION_DROP):
                n += 1
        return n

    def ids(self):
        with self._lock:
            return [i for i in self._order if i in self._held and not self._held[i].event.is_set()]

    def pending(self):
        """List of held requests for the GUI (oldest first)."""
        out = []
        with self._lock:
            for rid in list(self._order):
                item = self._held.get(rid)
                if item is None or item.event.is_set():
                    continue
                out.append({
                    "id": item.id,
                    "method": item.meta.get("method", "GET"),
                    "host": item.meta.get("host", ""),
                    "path": item.meta.get("path", "/"),
                    "scheme": item.meta.get("scheme", "http"),
                    "tls": item.meta.get("tls", False),
                    "source": item.meta.get("source", "proxy"),
                    "age": round(time.time() - item.ts, 1),
                    "raw": item.raw,
                })
        return out

    def get(self, rid):
        with self._lock:
            item = self._held.get(int(rid))
            if item is None:
                return None
            return {
                "id": item.id,
                "method": item.meta.get("method"),
                "host": item.meta.get("host"),
                "path": item.meta.get("path"),
                "raw": item.raw,
                "pending": not item.event.is_set(),
            }

    def count(self):
        with self._lock:
            return len([1 for i in self._held.values() if not i.event.is_set()])

    def cleanup(self):
        """Forget resolved entries older than a minute."""
        cutoff = time.time() - 60
        with self._lock:
            keep = []
            for rid in self._order:
                item = self._held.get(rid)
                if item is None:
                    continue
                if item.event.is_set() and item.ts < cutoff:
                    del self._held[rid]
                else:
                    keep.append(rid)
            self._order = keep


QUEUE = InterceptQueue()
