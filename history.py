#!/usr/bin/env python3
"""
PocketBurp -- history.py
SQLite-backed storage for every request/response that passes through the
proxy, plus helpers to rebuild raw HTTP text for the GUI.
"""

import gzip
import os
import sqlite3
import threading
import time
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE, "db")
DB_PATH = os.path.join(DB_DIR, "history.db")

MAX_BODY = 1_000_000  # 1 MB per body, keeps the DB small on a phone
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'proxy',
    tls          INTEGER NOT NULL DEFAULT 0,
    method       TEXT    NOT NULL DEFAULT '',
    host         TEXT    NOT NULL DEFAULT '',
    port         INTEGER NOT NULL DEFAULT 80,
    scheme       TEXT    NOT NULL DEFAULT 'http',
    path         TEXT    NOT NULL DEFAULT '/',
    req_headers  TEXT    NOT NULL DEFAULT '',
    req_body     BLOB,
    status       INTEGER NOT NULL DEFAULT 0,
    resp_headers TEXT    NOT NULL DEFAULT '',
    resp_body    BLOB,
    length       INTEGER NOT NULL DEFAULT 0,
    time_ms      INTEGER NOT NULL DEFAULT 0,
    in_scope     INTEGER NOT NULL DEFAULT 1,
    note         TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts DESC);
CREATE INDEX IF NOT EXISTS idx_requests_host ON requests(host);
CREATE TABLE IF NOT EXISTS scan_results (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id   TEXT NOT NULL,
    ts        REAL NOT NULL,
    attack    TEXT NOT NULL,
    payload   TEXT NOT NULL,
    point     TEXT NOT NULL,
    method    TEXT,
    url       TEXT,
    status    INTEGER,
    length    INTEGER,
    time_ms   INTEGER,
    reflected INTEGER,
    flags     TEXT
);
CREATE INDEX IF NOT EXISTS idx_scan_scan ON scan_results(scan_id);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        # some iSH / container filesystems refuse WAL -- rollback journal is fine
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error:
            pass
    return conn


def init():
    os.makedirs(DB_DIR, exist_ok=True)
    with _lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


def _clip(body):
    if body is None:
        return b""
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    return body[:MAX_BODY]


def log(source="proxy", tls=False, method="", host="", port=0, scheme="http",
        path="/", req_headers="", req_body=b"", status=0, resp_headers="",
        resp_body=b"", time_ms=0, in_scope=True, note=""):
    """Insert one completed exchange and return its row id."""
    init()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """INSERT INTO requests
                   (ts, source, tls, method, host, port, scheme, path,
                    req_headers, req_body, status, resp_headers, resp_body,
                    length, time_ms, in_scope, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), source, 1 if tls else 0, method, host,
                    int(port or 0), scheme, path, req_headers,
                    _clip(req_body), int(status or 0), resp_headers,
                    _clip(resp_body), len(resp_body or b""), int(time_ms or 0),
                    1 if in_scope else 0, note,
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def query(q=None, method=None, status=None, source=None, limit=200, before_id=None):
    """Filtered history list (no bodies, for table rendering)."""
    init()
    sql = ["SELECT id, ts, source, tls, method, host, port, scheme, path,",
           " status, length, time_ms, in_scope, note FROM requests WHERE 1=1"]
    args = []
    if q:
        sql.append(" AND (host LIKE ? OR path LIKE ? OR method LIKE ? OR note LIKE ?)")
        like = "%" + q + "%"
        args += [like, like, like, like]
    if method:
        sql.append(" AND method = ?")
        args.append(method.upper())
    if status:
        s = str(status)
        if len(s) == 3 and s.isdigit():
            sql.append(" AND status = ?")
            args.append(int(s))
        elif s[:1] in "12345" and s[:1].isdigit() and len(s) == 1:
            sql.append(" AND status BETWEEN ? AND ?")
            args += [int(s) * 100, int(s) * 100 + 99]
    if source:
        sql.append(" AND source = ?")
        args.append(source)
    if before_id:
        sql.append(" AND id < ?")
        args.append(int(before_id))
    sql.append(" ORDER BY id DESC LIMIT ?")
    args.append(int(limit))
    conn = _connect()
    try:
        rows = [dict(r) for r in conn.execute("".join(sql), args)]
    finally:
        conn.close()
    return rows


def get(req_id):
    """Full row incl. bodies + reconstructed raw request/response text."""
    init()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM requests WHERE id=?", (int(req_id),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    data = dict(row)
    req_headers = data.get("req_headers") or ""
    resp_headers = data.get("resp_headers") or ""
    # Some sources store the status line inside the header blob; strip it so the
    # GUI never renders it twice.
    if resp_headers.strip().upper().startswith("HTTP/"):
        resp_headers = resp_headers.split("\n", 1)[1] if "\n" in resp_headers else ""
    req_body = data.get("req_body") or b""
    resp_body = data.get("resp_body") or b""

    request_line = "%s %s HTTP/1.1" % (data.get("method") or "GET", data.get("path") or "/")
    data["request_raw"] = build_raw(request_line, req_headers, req_body)
    data["request_headers"] = req_headers
    data["request_body_text"] = display_body(req_headers, req_body)

    status = data.get("status") or 0
    if status:
        status_line = "HTTP/1.1 %s" % status
        data["status_line"] = status_line
        data["response_raw"] = build_raw(status_line, resp_headers, resp_body)
        data["response_body_text"] = display_body(resp_headers, resp_body)
    else:
        data["status_line"] = "HTTP/1.1 (no response -- dropped or failed)"
        data["response_raw"] = "%s\r\n\r\n%s" % (
            data["status_line"], data.get("note") or "")
        data["response_body_text"] = ""
    data["url"] = "%s://%s%s" % (data.get("scheme"), data.get("host"), data.get("path"))
    return data


def build_raw(start_line, headers_text, body_bytes):
    out = start_line + "\r\n"
    if headers_text:
        out += headers_text.rstrip("\r\n") + "\r\n"
    out += "\r\n"
    if body_bytes:
        out += (body_bytes if isinstance(body_bytes, str)
                else body_bytes.decode("latin-1"))
    return out


def display_body(headers_text, body_bytes):
    """Best-effort decode of a body for on-screen display (gzip/deflate aware)."""
    if not body_bytes:
        return ""
    raw = body_bytes if isinstance(body_bytes, bytes) else body_bytes.encode("latin-1", "replace")
    headers_text = (headers_text or "").lower()
    try:
        if "content-encoding: gzip" in headers_text or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        elif "content-encoding: deflate" in headers_text:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def count():
    init()
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM requests").fetchone()["c"]
    finally:
        conn.close()


def clear():
    init()
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM requests")
            conn.execute("DELETE FROM scan_results")
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------- scan rows

def log_scan_row(scan_id, attack, payload, point, method, url, status, length,
                 time_ms, reflected, flags):
    init()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO scan_results
                   (scan_id, ts, attack, payload, point, method, url, status,
                    length, time_ms, reflected, flags)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scan_id, time.time(), attack, payload, point, method, url,
                 int(status or 0), int(length or 0), int(time_ms or 0),
                 1 if reflected else 0, ",".join(flags or [])),
            )
            conn.commit()
        finally:
            conn.close()


def scan_rows(scan_id):
    init()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM scan_results WHERE scan_id=? ORDER BY id", (scan_id,)
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["flags"] = [f for f in (d.get("flags") or "").split(",") if f]
        out.append(d)
    return out
