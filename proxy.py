#!/usr/bin/env python3
"""
PocketBurp -- proxy.py
The pure-Python intercepting MITM proxy (HTTP + HTTPS + SOCKS5).

No root, no packet capture: this is an application-layer proxy. HTTPS is
decrypted by presenting per-host certificates signed by the PocketBurp CA
(see ca.py), exactly like Burp does.

Request lifecycle:
    client -> parse -> scope check -> hold in Intercept queue (optional)
           -> forward (possibly edited) -> target -> response -> log -> client
"""

import socket
import socketserver
import ssl
import threading
import time
import traceback

import ca
import history
import intercept
import scope

MAX_HEADER_BYTES = 128 * 1024
MAX_BODY = 8 * 1024 * 1024
CLIENT_TIMEOUT = 120
UPSTREAM_TIMEOUT = 30

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
LOOPBACK_PORTS = (8080, 9090)


# --------------------------------------------------------------------------
# HTTP message parsing (shared with repeater.py and scanner.py)
# --------------------------------------------------------------------------

class HttpRequest(object):
    def __init__(self):
        self.method = "GET"
        self.target = "/"
        self.version = "HTTP/1.1"
        self.headers = []          # list[str] "Name: value"
        self.body = b""            # decoded body bytes
        self.chunked = False


def parse_raw_request(text):
    """Parse raw HTTP request text (CRLF or LF) into an HttpRequest."""
    if isinstance(text, bytes):
        text = text.decode("latin-1")
    text = text.replace("\r\n", "\n")
    if "\n\n" in text:
        head, body_text = text.split("\n\n", 1)
    else:
        head, body_text = text, ""
    lines = head.split("\n")
    req = HttpRequest()
    if lines and lines[0].strip():
        parts = lines[0].split()
        if len(parts) >= 3:
            req.method, req.target, req.version = parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            req.method, req.target = parts[0], parts[1]
    for line in lines[1:]:
        if not line.strip():
            continue
        if ":" in line:
            req.headers.append(line.rstrip())
        elif line.startswith((" ", "\t")) and req.headers:
            req.headers[-1] += " " + line.strip()
    req.body = body_text.encode("latin-1", "replace") if body_text else b""
    return req


def header_get(headers, name, default=None):
    low = name.lower()
    for h in headers:
        if ":" in h:
            k, v = h.split(":", 1)
            if k.strip().lower() == low:
                return v.strip()
    return default


def header_set(headers, name, value):
    low = name.lower()
    out, replaced = [], False
    for h in headers:
        if ":" in h and h.split(":", 1)[0].strip().lower() == low:
            if not replaced:
                out.append("%s: %s" % (name, value))
                replaced = True
            continue
        out.append(h)
    if not replaced:
        out.append("%s: %s" % (name, value))
    return out


def header_del(headers, name):
    low = name.lower()
    return [h for h in headers
            if not (":" in h and h.split(":", 1)[0].strip().lower() == low)]


HOP_BY_HOP = {"proxy-connection", "proxy-authorization", "keep-alive", "te",
              "trailers", "upgrade"}


def clean_forward_headers(headers):
    return [h for h in headers
            if ":" in h and h.split(":", 1)[0].strip().lower() not in HOP_BY_HOP]


def split_target(target, headers, default_scheme=None, default_port=None):
    """Resolve a request target (origin-form or absolute-form) to
    (host, port, scheme, origin_path)."""
    scheme = None
    host = None
    port = None
    path = target or "/"

    if target and target.lower().startswith(("http://", "https://")):
        scheme, rest = target.split("://", 1)
        scheme = scheme.lower()
        authority, _, p = rest.partition("/")
        path = "/" + p if p else "/"
        if authority.startswith("["):
            host = authority[1:].split("]", 1)[0]
            if "]:" in authority:
                try:
                    port = int(authority.split("]:", 1)[1])
                except ValueError:
                    pass
        elif ":" in authority:
            h, port_s = authority.rsplit(":", 1)
            host = h
            try:
                port = int(port_s)
            except ValueError:
                pass
        else:
            host = authority

    if not host:
        host_header = (header_get(headers, "Host", "") or "").strip()
        if host_header.startswith("["):                      # [::1]:8443
            host = host_header[1:].split("]", 1)[0]
            if "]:" in host_header:
                try:
                    port = int(host_header.split("]:", 1)[1])
                except ValueError:
                    pass
        elif host_header.count(":") == 1:
            h, port_s = host_header.rsplit(":", 1)
            host = h
            try:
                port = int(port_s)
            except ValueError:
                host = host_header
        elif host_header:
            host = host_header

    if port < 1 if isinstance(port, int) else False:
        port = None
    if scheme is None:
        scheme = default_scheme or ("https" if port == 443 else "http")
    if port is None:
        port = default_port or (443 if scheme == "https" else 80)
    if not path:
        path = "/"
    return host or "", int(port), scheme, path


# --------------------------------------------------------------------------
# Low-level socket helpers
# --------------------------------------------------------------------------

def _read_headers(f):
    headers = []
    total = 0
    while True:
        line = f.readline(65537)
        if not line:
            return headers
        total += len(line)
        if total > MAX_HEADER_BYTES or len(headers) > 300:
            raise ValueError("header section too large")
        if line in (b"\r\n", b"\n"):
            return headers
        headers.append(line.decode("latin-1").rstrip("\r\n"))


def _read_chunked(f):
    body = b""
    while len(body) < MAX_BODY:
        line = f.readline(65537)
        if not line:
            break
        try:
            size = int(line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            break
        if size == 0:
            while True:
                trailer = f.readline(65537)
                if trailer in (b"\r\n", b"\n", b""):
                    break
            break
        body += f.read(size)
        f.readline(65537)   # trailing CRLF
    return body


def _read_body(f, headers, method="GET", status=None):
    """Read a body per Transfer-Encoding / Content-Length / until-close.
    Returns (decoded_body, was_chunked)."""
    te = (header_get(headers, "Transfer-Encoding", "") or "").lower()
    if status is not None and (method == "HEAD" or status in (204, 304)
                               or 100 <= status < 200):
        return b"", False
    if "chunked" in te:
        return _read_chunked(f), True
    cl = header_get(headers, "Content-Length")
    if cl is not None:
        try:
            n = int(cl.split(",")[0].strip())
        except ValueError:
            n = 0
        n = max(0, min(n, MAX_BODY))
        chunks, got = [], 0
        while got < n:
            chunk = f.read(n - got)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks), False
    if status is None:
        return b"", False          # request with no CL/TE has no body
    chunks, got = [], 0
    while got < MAX_BODY:           # response body delimited by connection close
        chunk = f.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks), False


def _connect_upstream(host, port, timeout=UPSTREAM_TIMEOUT):
    return socket.create_connection((host, int(port)), timeout=timeout)


def _tls_upstream(sock, host):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_alpn_protocols(["http/1.1"])
    except Exception:
        pass
    return ctx.wrap_socket(sock, server_hostname=host)


def _status_text(code):
    return {
        200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently",
        302: "Found", 304: "Not Modified", 400: "Bad Request", 403: "Forbidden",
        404: "Not Found", 405: "Method Not Allowed",
        407: "Proxy Authentication Required", 500: "Internal Server Error",
        502: "Bad Gateway", 504: "Gateway Timeout",
    }.get(code, "Unknown")


def build_response(status, reason, headers, body):
    head = "\r\n".join(["HTTP/1.1 %d %s" % (status, reason)] + list(headers))
    return (head + "\r\n\r\n").encode("latin-1") + (body or b"")


def error_page(status, title, detail):
    body = (
        "<!doctype html><meta name=viewport content='width=device-width'>"
        "<body style='background:#1e1e1e;color:#ddd;font-family:-apple-system,"
        "sans-serif;padding:24px'><h2 style='color:#ff6633'>PocketBurp</h2>"
        "<h3>%s %s</h3><pre style='white-space:pre-wrap;color:#aaa'>%s</pre>"
        "</body>" % (status, title, detail)
    ).encode("utf-8")
    return build_response(status, _status_text(status), [
        "Content-Type: text/html; charset=utf-8",
        "Content-Length: %d" % len(body),
        "Connection: close",
        "Cache-Control: no-store",
    ], body)


def _raw_with_headers(req, headers):
    head = "%s %s %s\r\n" % (req.method, req.target, req.version or "HTTP/1.1")
    head += "\r\n".join(headers)
    return head + "\r\n\r\n" + req.body.decode("latin-1", "replace")


# --------------------------------------------------------------------------
# Sending one request upstream (used by proxy, repeater and scanner)
# --------------------------------------------------------------------------

def send_raw_request(raw_text, default_host=None, default_port=None,
                     default_scheme=None, timeout=UPSTREAM_TIMEOUT):
    """Send raw HTTP request text to its target and return the exchange."""
    started = time.time()
    req = parse_raw_request(raw_text)
    host, port, scheme, path = split_target(
        req.target, req.headers, default_scheme, default_port)
    if not host:
        return {"ok": False, "error": "No Host header / target in request",
                "status": 0, "headers": "", "body": b"", "time_ms": 0,
                "raw": "", "scheme": scheme or "http", "host": "", "port": port or 80,
                "path": path, "method": req.method}

    headers = clean_forward_headers(req.headers)
    host_header = header_get(req.headers, "Host", "")
    if not host_header:
        host_header = host if port in (80, 443) and (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ) else "%s:%d" % (host, port)
    headers = header_set(headers, "Host", host_header)

    # Ask for encodings we can decode with the stdlib so the GUI never shows
    # binary soup (Brotli has no stdlib decoder; and gzip is enough on a phone).
    if header_get(headers, "Accept-Encoding") is not None:
        headers = header_set(headers, "Accept-Encoding", "gzip, deflate")

    body = req.body
    had_length = header_get(headers, "Content-Length") is not None
    if req.chunked or any(h.lower().startswith("transfer-encoding:") for h in headers):
        headers = header_del(headers, "Transfer-Encoding")
        headers = header_set(headers, "Content-Length", str(len(body)))
    elif had_length or body:
        # The request may have been edited in the Intercept tab: keep
        # Content-Length truthful even if the user changed the body length.
        current = header_get(headers, "Content-Length")
        if current is None or current.strip() != str(len(body)):
            headers = header_set(headers, "Content-Length", str(len(body)))
    headers = header_set(headers, "Connection", "close")

    wire = ("%s %s %s\r\n" % (req.method, path, req.version or "HTTP/1.1")).encode("latin-1")
    wire += ("\r\n".join(headers) + "\r\n\r\n").encode("latin-1") + body

    sock = None
    try:
        sock = _connect_upstream(host, port, timeout=timeout)
        if scheme == "https":
            sock = _tls_upstream(sock, host)
        sock.sendall(wire)
        f = sock.makefile("rb")
        status_line = f.readline(65537).decode("latin-1").strip()
        if not status_line:
            raise IOError("empty response from target")
        parts = status_line.split(None, 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        resp_headers = _read_headers(f)
        resp_body, _ = _read_body(f, resp_headers, req.method, status)
        elapsed = int((time.time() - started) * 1000)
        hdrs_text = "\r\n".join(resp_headers)
        raw = (status_line + "\r\n" + hdrs_text + "\r\n\r\n"
               + history.display_body(hdrs_text, resp_body))
        return {
            "ok": True, "status": status, "status_line": status_line,
            "headers": hdrs_text, "body": resp_body, "time_ms": elapsed,
            "raw": raw, "scheme": scheme, "host": host, "port": port,
            "path": path, "method": req.method,
        }
    except Exception as exc:
        return {
            "ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
            "status": 0, "headers": "", "body": b"",
            "time_ms": int((time.time() - started) * 1000),
            "raw": "PocketBurp: request failed\n\n%s" % exc,
            "scheme": scheme, "host": host, "port": port, "path": path,
            "method": req.method,
        }
    finally:
        try:
            if sock:
                sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Proxy engine
# --------------------------------------------------------------------------

class ProxyEngine(object):
    def __init__(self, host="0.0.0.0", port=8080):
        self.host = host
        self.port = int(port)
        self.server = None
        self.thread = None
        self.running = False
        self.started_at = None
        self.last_error = ""
        self.stats = {
            "requests": 0, "forwarded": 0, "dropped": 0, "errors": 0,
            "https": 0, "http": 0, "socks": 0, "connections": 0,
        }

    # ---------------------------------------------------------- lifecycle
    def start(self):
        if self.running:
            return False, "already running"
        try:
            ca.ensure_ca()
        except ca.CAError as exc:
            return False, "CA error: %s" % exc
        try:
            self.server = _ThreadedHTTPServer((self.host, self.port), _make_handler(self))
        except OSError as exc:
            self.last_error = str(exc)
            return False, "bind failed on port %d: %s" % (self.port, exc)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="pocketburp-proxy", daemon=True)
        self.thread.start()
        self.running = True
        self.started_at = time.time()
        return True, "listening on %s:%d" % (self.host, self.port)

    def stop(self):
        if not self.running:
            return False, "not running"
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.running = False
        self.server = None
        return True, "stopped"

    def status(self):
        return {
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "uptime": int(time.time() - self.started_at)
            if (self.started_at and self.running) else 0,
            "stats": dict(self.stats),
            "last_error": self.last_error,
        }

    # ------------------------------------------------------ shared pipeline
    def handle_exchange(self, meta, raw_text, send_back):
        """Intercept (optional) -> forward -> log -> write response back.

        Returns (keep_alive, note)."""
        in_scope = scope.is_in_scope(meta["host"])
        silent = scope.enforced() and not in_scope
        should_hold = (not silent) and intercept.QUEUE.should_hold(meta)
        note = ""
        final_raw = raw_text

        if should_hold:
            self.stats["requests"] += 1
            action, edited = intercept.QUEUE.add(meta)
            if action == intercept.ACTION_DROP:
                self.stats["dropped"] += 1
                history.log(source=meta.get("source", "proxy"), tls=meta.get("tls", False),
                            method=meta["method"], host=meta["host"], port=meta["port"],
                            scheme=meta["scheme"], path=meta["path"],
                            req_headers="\r\n".join(meta["headers"]),
                            req_body=meta["body"], status=0,
                            in_scope=in_scope, note="Dropped in Intercept")
                return False, "dropped"
            if edited and edited != raw_text:
                note = "Edited in Intercept"
                final_raw = edited
            else:
                note = "Intercept: forwarded"
        if silent:
            self.stats["requests"] += 1

        result = send_raw_request(
            final_raw, default_host=meta["host"], default_port=meta["port"],
            default_scheme=meta["scheme"])

        if not result["ok"]:
            self.stats["errors"] += 1
            history.log(
                source=meta.get("source", "proxy"), tls=meta.get("tls", False),
                method=result.get("method") or meta["method"],
                host=result.get("host") or meta["host"],
                port=result.get("port") or meta["port"],
                scheme=result.get("scheme") or meta["scheme"],
                path=result.get("path") or meta["path"],
                req_headers="\r\n".join(meta["headers"]), req_body=meta["body"],
                status=0, time_ms=result.get("time_ms", 0), in_scope=in_scope,
                note=(note + " | " if note else "") + result["error"])
            return False, result["error"]

        self.stats["forwarded"] += 1
        if not silent:
            sent = parse_raw_request(final_raw)
            history.log(
                source=meta.get("source", "proxy"), tls=meta.get("tls", False),
                method=result.get("method") or meta["method"],
                host=result.get("host") or meta["host"],
                port=result.get("port") or meta["port"],
                scheme=result.get("scheme") or meta["scheme"],
                path=result.get("path") or meta["path"],
                req_headers="\r\n".join(sent.headers), req_body=sent.body,
                status=result["status"], resp_headers=result["headers"],
                resp_body=result["body"], time_ms=result["time_ms"],
                in_scope=in_scope, note=note)

        resp_headers = result["headers"].split("\r\n") if result["headers"] else []
        try:
            send_back(result["status"], resp_headers, result["body"])
        except Exception:
            return False, "client write failed"
        return True, "ok"


class _ThreadedHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request, client_address):
        pass   # never spam tracebacks on a phone


def _make_handler(engine):
    class Handler(socketserver.BaseRequestHandler):
        # ------------------------------------------------------------ entry
        def handle(self):
            engine.stats["connections"] += 1
            sock = self.request
            sock.settimeout(CLIENT_TIMEOUT)
            try:
                try:
                    peeked = sock.recv(1, socket.MSG_PEEK)
                except (socket.timeout, TimeoutError, OSError):
                    return
                if not peeked:
                    return
                if peeked == b"\x05":                          # SOCKS5 greeting
                    self._socks5(engine, sock)
                    return
                f = sock.makefile("rb")
                first = f.readline(65537)
                if not first:
                    return
                if first.upper().startswith(b"CONNECT "):
                    self._connect(engine, sock, f, first)
                    return
                if not first.upper().startswith((b"GET ", b"POST ", b"PUT ",
                                                 b"DELETE ", b"HEAD ", b"OPTIONS ",
                                                 b"PATCH ", b"TRACE ")):
                    return
                self._http_loop(engine, sock, f, first, tls=False,
                                fixed_host=None, fixed_port=None)
            except (ssl.SSLError, ConnectionResetError, BrokenPipeError,
                    socket.timeout, TimeoutError):
                return
            except Exception:
                engine.last_error = traceback.format_exc(limit=3)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

        # ------------------------------------------------------- CONNECT
        def _connect(self, engine, sock, f, first_line):
            target = first_line.split()[1].decode("latin-1") if len(first_line.split()) > 1 else ""
            host, port = target, 443
            if target.startswith("["):
                host = target[1:].split("]", 1)[0]
                if "]:" in target:
                    try:
                        port = int(target.split("]:", 1)[1])
                    except ValueError:
                        port = 443
            elif ":" in target:
                h, p = target.rsplit(":", 1)
                host = h
                try:
                    port = int(p)
                except ValueError:
                    port = 443

            if host.lower() in LOOPBACK_HOSTS and port in LOOPBACK_PORTS:
                self._send(sock, error_page(
                    502, "Proxy loop detected",
                    "Safari asked the proxy to fetch PocketBurp's own GUI.\n"
                    "Just open http://127.0.0.1:9090 directly."))
                return
            try:
                ctx = ca.ssl_context_for_host(host)
            except Exception as exc:
                self._send(sock, error_page(
                    502, "Cannot forge a certificate for %s" % host, str(exc)))
                return
            self._send(sock, b"HTTP/1.1 200 Connection established\r\n"
                             b"Proxy-Agent: PocketBurp\r\n\r\n")
            try:
                tls_sock = ctx.wrap_socket(sock, server_side=True)
            except (ssl.SSLError, OSError) as exc:
                engine.stats["errors"] += 1
                history.log(source="proxy", tls=True, method="CONNECT", host=host,
                            port=port, scheme="https", path="/", status=0,
                            note="TLS handshake failed (%s). Is the PocketBurp CA "
                                 "installed AND trusted in Certificate Trust Settings?"
                                 % type(exc).__name__)
                return
            engine.stats["https"] += 1
            try:
                tf = tls_sock.makefile("rb")
                line = tf.readline(65537)
                if line:
                    self._http_loop(engine, tls_sock, tf, line, tls=True,
                                    fixed_host=host, fixed_port=port)
            finally:
                try:
                    tf.close()
                except Exception:
                    pass
                try:
                    tls_sock.close()
                except Exception:
                    pass

        # ---------------------------------------------------- HTTP request loop
        def _http_loop(self, engine, sock, f, first_line, tls, fixed_host, fixed_port):
            line = first_line
            while line:
                req = self._parse_request(f, line)
                if req is None:
                    return
                host, port, scheme, path = split_target(req.target, req.headers)
                if fixed_host:
                    host, port, scheme = fixed_host, fixed_port or port, "https"
                    if not req.target.lower().startswith(("http://", "https://")):
                        path = req.target
                if host.lower() in LOOPBACK_HOSTS and port in LOOPBACK_PORTS:
                    self._send(sock, error_page(
                        502, "Proxy loop detected",
                        "Request aimed at PocketBurp itself (%s:%d)." % (host, port)))
                    return

                headers = list(req.headers)
                if req.chunked:
                    headers = header_del(headers, "Transfer-Encoding")
                    headers = header_set(headers, "Content-Length", str(len(req.body)))
                if not header_get(headers, "Host"):
                    headers.insert(0, "Host: %s" % (
                        host if port in (80, 443) else "%s:%d" % (host, port)))

                meta = {
                    "method": req.method, "path": path, "host": host,
                    "port": port, "scheme": scheme, "tls": tls,
                    "headers": headers, "body": req.body, "source": "proxy",
                    "body_preview": history.display_body(
                        "\r\n".join(headers), req.body)[:300000],
                }
                raw_text = _raw_with_headers(req, headers)
                engine.stats["https" if tls else "http"] += 1

                if (header_get(headers, "Upgrade") or "").lower() == "websocket":
                    self._websocket(engine, sock, f, req, headers, host, port,
                                    scheme, path, tls)
                    return

                method = req.method
                client_close = "close" in (
                    header_get(req.headers, "Connection", "") or "").lower()

                def send_back(status, resp_headers, body, _sock=sock, _m=method,
                              _close=client_close):
                    relayed = self._relay_headers(_m, status, resp_headers, body,
                                                  keep_alive=not _close)
                    self._send(_sock, build_response(status, _status_text(status),
                                                     relayed, body))

                keep_alive, _note = engine.handle_exchange(meta, raw_text, send_back)
                if not keep_alive or client_close:
                    return
                try:
                    line = f.readline(65537)
                except (socket.timeout, TimeoutError, ssl.SSLError, OSError):
                    return

        def _websocket(self, engine, sock, f, req, headers, host, port, scheme,
                       path, tls):
            """Pass an Upgrade (WebSocket) handshake straight to the target and
            then splice the two sockets. Frames are not decoded/logged."""
            upstream = None
            try:
                upstream = _connect_upstream(host, port)
                if scheme == "https":
                    upstream = _tls_upstream(upstream, host)
                wire = ("%s %s %s\r\n" % (req.method, path, req.version)).encode("latin-1")
                wire += ("\r\n".join(clean_forward_headers(headers)) + "\r\n\r\n").encode("latin-1")
                upstream.sendall(wire + req.body)
                uf = upstream.makefile("rb")
                status_line = uf.readline(65537)
                resp_headers = _read_headers(uf)
                status = 0
                try:
                    status = int(status_line.split()[1])
                except Exception:
                    pass
                sock.sendall(status_line + ("\r\n".join(resp_headers) + "\r\n\r\n").encode("latin-1"))
                history.log(source="proxy", tls=tls, method=req.method, host=host,
                            port=port, scheme=scheme, path=path,
                            req_headers="\r\n".join(headers), req_body=req.body,
                            status=status, resp_headers="\r\n".join(resp_headers),
                            resp_body=b"", time_ms=0,
                            note="WebSocket upgrade -- tunnel is raw, frames are not decoded")
                if status != 101:
                    return
                engine.stats["forwarded"] += 1
                self._pipe_streams(f, sock, uf, upstream)
            except Exception:
                engine.stats["errors"] += 1
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass

        def _pipe_streams(self, client_f, client_sock, upstream_f, upstream_sock):
            def up():
                try:
                    while True:
                        data = client_f.read1(65536)
                        if not data:
                            break
                        upstream_sock.sendall(data)
                except Exception:
                    pass
                finally:
                    try:
                        upstream_sock.shutdown(socket.SHUT_WR)
                    except Exception:
                        pass
            t = threading.Thread(target=up, daemon=True)
            t.start()
            try:
                while True:
                    data = upstream_f.read1(65536)
                    if not data:
                        break
                    client_sock.sendall(data)
            except Exception:
                pass
            t.join(timeout=5)

        def _relay_headers(self, method, status, headers, body, keep_alive=True):
            """Re-frame a (de-chunked or close-delimited) response for the client."""
            out = [h for h in headers
                   if ":" in h and h.split(":", 1)[0].strip().lower()
                   not in ("connection", "keep-alive", "transfer-encoding")]
            if method == "HEAD" or status in (204, 304) or 100 <= status < 200:
                if not any(h.lower().startswith("content-length:") for h in out):
                    pass
            else:
                out = [h for h in out if not h.lower().startswith("content-length:")]
                out.append("Content-Length: %d" % len(body))
            out.append("Connection: keep-alive" if keep_alive else "Connection: close")
            return out

        def _parse_request(self, f, first_line):
            text = first_line.decode("latin-1").rstrip("\r\n")
            parts = text.split()
            if len(parts) < 2:
                return None
            req = HttpRequest()
            req.method = parts[0]
            req.target = parts[1]
            req.version = parts[2] if len(parts) > 2 else "HTTP/1.1"
            req.headers = _read_headers(f)
            req.body, req.chunked = _read_body(f, req.headers, req.method, None)
            return req

        def _send(self, sock, data):
            try:
                sock.sendall(data)
            except Exception:
                pass

        # ------------------------------------------------------------ SOCKS5
        def _recv_exact(self, sock, n):
            data = b""
            while len(data) < n:
                chunk = sock.recv(n - len(data))
                if not chunk:
                    raise IOError("peer closed during SOCKS handshake")
                data += chunk
            return data

        def _socks5(self, engine, sock):
            """Accept SOCKS5 CONNECT (used by apps that prefer SOCKS over the
            Wi-Fi HTTP proxy, e.g. UTM-SE / LiveContainer guests)."""
            try:
                if self._recv_exact(sock, 1)[0] != 5:
                    return
                nmethods = self._recv_exact(sock, 1)[0]
                self._recv_exact(sock, nmethods)
                sock.sendall(b"\x05\x00")                     # NO AUTH
                head = self._recv_exact(sock, 4)
                if head[1] != 0x01:                             # CONNECT only
                    sock.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                    return
                atyp = head[3]
                if atyp == 0x01:
                    host = socket.inet_ntoa(self._recv_exact(sock, 4))
                elif atyp == 0x03:
                    host = self._recv_exact(sock, self._recv_exact(sock, 1)[0]).decode("latin-1")
                elif atyp == 0x04:
                    host = socket.inet_ntop(socket.AF_INET6, self._recv_exact(sock, 16))
                else:
                    return
                port = int.from_bytes(self._recv_exact(sock, 2), "big")
            except Exception:
                return

            try:
                sock.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                engine.stats["socks"] += 1
                try:
                    first3 = sock.recv(3, socket.MSG_PEEK)
                except Exception:
                    first3 = b""

                if first3[:1] == b"\x16":                      # TLS -> decrypted MITM
                    try:
                        ctx = ca.ssl_context_for_host(host)
                        tls_sock = ctx.wrap_socket(sock, server_side=True)
                    except (ssl.SSLError, OSError) as exc:
                        engine.stats["errors"] += 1
                        history.log(source="socks", tls=True, method="CONNECT", host=host,
                                    port=port, scheme="https", path="/", status=0,
                                    note="SOCKS TLS handshake failed (%s). Is the CA "
                                         "installed and trusted?" % type(exc).__name__)
                        return
                    engine.stats["https"] += 1
                    tf = tls_sock.makefile("rb")
                    try:
                        line = tf.readline(65537)
                        if line:
                            self._http_loop(engine, tls_sock, tf, line, True, host, port)
                    finally:
                        try:
                            tf.close()
                        except Exception:
                            pass
                        try:
                            tls_sock.close()
                        except Exception:
                            pass
                    return

                # plain / non-HTTP: raw byte tunnel (only now do we need upstream)
                try:
                    upstream = _connect_upstream(host, port)
                except Exception:
                    engine.stats["errors"] += 1
                    return
                engine.stats["tunnels"] = engine.stats.get("tunnels", 0) + 1
                history.log(source="socks", tls=False, method="TUNNEL", host=host,
                            port=port, scheme="tcp", path="/", status=0,
                            note="SOCKS tunnel (raw bytes, not decoded)")
                try:
                    self._pipe(sock, upstream)
                finally:
                    try:
                        upstream.close()
                    except Exception:
                        pass
            except Exception:
                engine.stats["errors"] += 1

        def _pipe(self, a, b):
            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except Exception:
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except Exception:
                        pass
            t = threading.Thread(target=pump, args=(b, a), daemon=True)
            t.start()
            pump(a, b)
            t.join(timeout=5)

    return Handler


# --------------------------------------------------------------------------
if __name__ == "__main__":
    history.init()
    eng = ProxyEngine("0.0.0.0", 8080)
    ok, msg = eng.start()
    print("PocketBurp proxy:", msg)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        eng.stop()
