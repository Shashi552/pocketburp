#!/usr/bin/env python3
"""
PocketBurp -- api.py
Zero-dependency REST + static-file server (port 9090) that bridges the Safari
GUI to the proxy engine.

Deliberately built on http.server rather than Flask: Flask + Werkzeug is slow
to install and memory-hungry on iSH, and this API needs nothing more than
JSON in / JSON out.
"""

import json
import os
import re
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ca
import history
import intercept
import repeater
import scanner
import scope

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE, "static")

CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
}


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "PocketBurp/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        pass

    @property
    def engine(self):
        return self.server.engine

    def _send_bytes(self, data, ctype="application/octet-stream", status=200,
                    extra=None, head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, *")
        extra_lower = {k.lower() for k in (extra or {})}
        if "cache-control" not in extra_lower:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, status=200, head_only=False):
        data = json.dumps(obj, default=str).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status,
                         head_only=head_only)

    def _err(self, msg, status=400):
        self._json({"error": str(msg)}, status=status)

    def _body_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 8 * 1024 * 1024))
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            # tolerate form-encoded bodies too
            parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            return {k: v[0] for k, v in parsed.items()}

    # ------------------------------------------------------------- dispatch
    def do_HEAD(self):
        self._dispatch("GET", head_only=True)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._send_bytes(b"", "text/plain", 204)

    def _dispatch(self, method, head_only=False):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._route_api(method, path, query, head_only)
            else:
                self._serve_static(path, head_only)
        except Exception as exc:
            if os.environ.get("POCKETBURP_DEBUG"):
                traceback.print_exc()
            try:
                self._err("%s: %s" % (type(exc).__name__, exc), 500)
            except Exception:
                pass

    # ----------------------------------------------------------- static GUI
    # Files that must live at the site root for PWA scope rules
    ROOT_ASSETS = {
        "/manifest.webmanifest": "manifest.webmanifest",
        "/sw.js": "sw.js",
        "/apple-touch-icon.png": os.path.join("icons", "icon-180.png"),
        "/apple-touch-icon-precomposed.png": os.path.join("icons", "icon-180.png"),
        "/favicon.ico": os.path.join("icons", "favicon-32.png"),
        "/favicon.png": os.path.join("icons", "favicon-32.png"),
    }

    def _serve_static(self, path, head_only=False):
        if path in ("/", "/index.html"):
            file_path = os.path.join(STATIC_DIR, "index.html")
        elif path in self.ROOT_ASSETS:
            file_path = os.path.join(STATIC_DIR, self.ROOT_ASSETS[path])
        else:
            rel = path.lstrip("/")
            if rel.startswith("static/"):
                rel = rel[len("static/"):]     # /static/icons/x.png -> icons/x.png
            file_path = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not file_path.startswith(STATIC_DIR):
                self._err("forbidden", 403)
                return
        if not os.path.isfile(file_path):
            self._send_bytes(b"Not found", "text/plain", 404)
            return
        ext = os.path.splitext(file_path)[1].lower()
        with open(file_path, "rb") as fh:
            data = fh.read()
        extra = {}
        if file_path.endswith("sw.js"):
            # allow the worker to control the whole origin, never cache it
            extra["Service-Worker-Allowed"] = "/"
            extra["Cache-Control"] = "no-cache, no-store, must-revalidate"
        self._send_bytes(data, CTYPES.get(ext, "application/octet-stream"),
                         head_only=head_only, extra=extra)

    # ---------------------------------------------------------------- routes
    def _route_api(self, method, path, query, head_only=False):
        engine = self.engine

        def q(name, default=None):
            return (query.get(name) or [default])[0]

        # ---------------------------------------------------------- status
        if path == "/api/status":
            scope_state = scope.get()
            return self._json({
                "proxy": engine.status(),
                "intercept": {
                    "enabled": intercept.QUEUE.enabled,
                    "pending": intercept.QUEUE.count(),
                    "auto_skip_static": intercept.QUEUE.auto_skip_static,
                    "stats": dict(intercept.QUEUE.stats),
                },
                "history_count": history.count(),
                "scope": {
                    "entries": scope_state["entries"],
                    "enforced": scope_state["enforced"],
                    "count": len(scope_state["entries"]),
                },
                "gui_port": self.server.server_address[1],
            }, head_only=head_only)

        # -------------------------------------------------------- intercept
        if path == "/api/intercept/toggle" and method == "POST":
            body = self._body_json()
            if "enabled" in body and body["enabled"] is not None:
                intercept.QUEUE.enabled = bool(body["enabled"])
            else:
                intercept.QUEUE.enabled = not intercept.QUEUE.enabled
            return self._json({"enabled": intercept.QUEUE.enabled,
                               "pending": intercept.QUEUE.count()})

        if path == "/api/intercept/autoskip" and method == "POST":
            body = self._body_json()
            if "enabled" in body:
                intercept.QUEUE.auto_skip_static = bool(body["enabled"])
            return self._json({"auto_skip_static": intercept.QUEUE.auto_skip_static})

        if path == "/api/queue" and method == "GET":
            return self._json({"pending": intercept.QUEUE.pending(),
                               "count": intercept.QUEUE.count()})

        m = re.match(r"^/api/queue/(\d+)/(forward|drop|edit)$", path)
        if m and method == "POST":
            rid, action = int(m.group(1)), m.group(2)
            body = self._body_json() if action == "edit" else {}
            if action == "drop":
                ok = intercept.QUEUE.resolve(rid, intercept.ACTION_DROP)
            elif action == "forward":
                ok = intercept.QUEUE.resolve(rid, intercept.ACTION_FORWARD)
            else:
                raw = body.get("raw") or ""
                if not raw.strip():
                    return self._err("empty request body")
                ok = intercept.QUEUE.resolve(rid, intercept.ACTION_FORWARD,
                                             edited_raw=raw)
            return self._json({"ok": ok, "pending": intercept.QUEUE.count()})

        if path in ("/api/queue/forward_all", "/api/queue/drop_all") and method == "POST":
            if path.endswith("forward_all"):
                n = intercept.QUEUE.forward_all()
            else:
                n = intercept.QUEUE.drop_all()
            return self._json({"released": n, "pending": intercept.QUEUE.count()})

        # ------------------------------------------------------------ proxy
        if path == "/api/proxy/start" and method == "POST":
            body = self._body_json()
            if body.get("port") and not engine.running:
                try:
                    engine.port = int(body["port"])
                except (TypeError, ValueError):
                    return self._err("bad port")
            ok, msg = engine.start()
            return self._json({"ok": ok, "message": msg,
                               "proxy": engine.status()})
        if path == "/api/proxy/stop" and method == "POST":
            ok, msg = engine.stop()
            return self._json({"ok": ok, "message": msg,
                               "proxy": engine.status()})

        # ---------------------------------------------------------- history
        if path == "/api/history" and method == "GET":
            rows = history.query(
                q=q("q"), method=q("method"), status=q("status"),
                limit=int(q("limit", "200") or 200),
                source=q("source"), before_id=q("before_id"))
            return self._json({"rows": rows, "count": len(rows),
                               "total": history.count()})
        if path == "/api/history/clear" and method == "POST":
            history.clear()
            return self._json({"ok": True})
        m = re.match(r"^/api/history/(\d+)$", path)
        if m and method == "GET":
            row = history.get(int(m.group(1)))
            if row is None:
                return self._err("no such request", 404)
            return self._json(row)

        # --------------------------------------------------------- repeater
        if path == "/api/repeater/send" and method == "POST":
            body = self._body_json()
            raw = body.get("raw") or ""
            host = body.get("host") or None
            port = body.get("port") or None
            scheme = body.get("scheme") or None
            if not raw.strip() and body.get("id"):
                row = history.get(int(body["id"]))
                if row is None:
                    return self._err("no such history id", 404)
                raw = row["request_raw"]
                host = host or row["host"]
                port = port or row["port"]
                scheme = scheme or row["scheme"]
            if not raw.strip():
                return self._err("empty request")
            result = repeater.send(raw, host=host, port=port, scheme=scheme)
            return self._json(result)
        if path == "/api/repeater/history" and method == "GET":
            return self._json({"sends": repeater.history()})
        m = re.match(r"^/api/repeater/entry/(\d+)$", path)
        if m and method == "GET":
            entry = repeater.get(int(m.group(1)))
            if entry is None:
                return self._err("no such send", 404)
            return self._json(entry)
        if path == "/api/repeater/clear" and method == "POST":
            repeater.clear()
            return self._json({"ok": True})

        # ---------------------------------------------------------- scanner
        if path == "/api/scanner/run" and method == "POST":
            body = self._body_json()
            raw = body.get("raw") or ""
            host = body.get("host") or None
            port = body.get("port") or None
            scheme = body.get("scheme") or None
            if not raw.strip() and body.get("request_id"):
                row = history.get(int(body["request_id"]))
                if row is None:
                    return self._err("no such history id", 404)
                raw = row["request_raw"]
                host = host or row["host"]
                port = port or row["port"]
                scheme = scheme or row["scheme"]
            if not raw.strip():
                return self._err("pick a request from history first")
            scan_id = scanner.start(
                raw, body.get("attack", "traversal"),
                custom_wordlist=body.get("wordlist", ""),
                points=body.get("points"),
                host=host, port=port, scheme=scheme,
                label=body.get("label", ""))
            return self._json({"scan_id": scan_id, "ok": True})
        if path == "/api/scanner/results" and method == "GET":
            scan_id = q("scan_id") or scanner.latest()
            if not scan_id:
                return self._json({"status": "idle", "results": [], "total": 0,
                                   "done": 0})
            state = scanner.get(scan_id)
            if state is None:
                return self._json({"status": "idle", "results": [], "total": 0,
                                   "done": 0})
            state["results"] = state["results"][-400:]
            return self._json(state)
        if path == "/api/scanner/payloads" and method == "GET":
            return self._json({k: v for k, v in scanner.PAYLOADS.items()})

        # ------------------------------------------------------------ scope
        if path == "/api/scope" and method == "GET":
            return self._json(scope.get())
        if path == "/api/scope" and method == "POST":
            body = self._body_json()
            state = scope.save(body.get("raw", ""),
                               body.get("enforced"))
            return self._json(state)

        # --------------------------------------------------------------- ca
        if path == "/api/ca/info" and method == "GET":
            info = ca.ca_info()
            info.pop("pem", None)
            return self._json(info)
        if path == "/api/ca/pem" and method == "GET":
            info = ca.ca_info()
            return self._send_bytes((info.get("pem") or "").encode(),
                                    "text/plain; charset=utf-8")
        if path in ("/api/ca/download", "/api/ca/mobileconfig", "/api/ca/profile.mobileconfig") and method == "GET":
            # If explicit crt requested via ?file=1 or ?crt=1 or ?format=crt
            if query.get("file") or query.get("crt") or (query.get("format") or [""])[0] == "crt":
                ca.ensure_ca()
                with open(ca.CA_CRT, "rb") as fh:
                    data = fh.read()
                return self._send_bytes(
                    data, "application/x-x509-ca-cert",
                    extra={"Content-Disposition": 'attachment; filename="pocketburp-ca.crt"'})

            # Default: Apple Configuration Profile (.mobileconfig) for direct one-tap iOS installation
            ca.ensure_ca()
            with open(ca.CA_CRT, "rb") as fh:
                pem_str = fh.read().decode("ascii", "replace")
            import ssl, base64, uuid
            der_bytes = ssl.PEM_cert_to_DER_cert(pem_str)
            b64_der = base64.b64encode(der_bytes).decode("ascii")
            p_uuid = str(uuid.uuid4()).upper()
            c_uuid = str(uuid.uuid4()).upper()
            mobileconfig = (
                "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
                "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
                "<plist version=\"1.0\">\n<dict>\n"
                "    <key>PayloadContent</key>\n    <array>\n        <dict>\n"
                "            <key>PayloadCertificateFileName</key>\n"
                "            <string>pocketburp-ca.crt</string>\n"
                "            <key>PayloadContent</key>\n"
                "            <data>" + b64_der + "</data>\n"
                "            <key>PayloadDescription</key>\n"
                "            <string>PocketBurp Root CA</string>\n"
                "            <key>PayloadDisplayName</key>\n"
                "            <string>PocketBurp Root CA</string>\n"
                "            <key>PayloadIdentifier</key>\n"
                "            <string>com.shashank.pocketburp.rootca</string>\n"
                "            <key>PayloadType</key>\n"
                "            <string>com.apple.security.root</string>\n"
                "            <key>PayloadUUID</key>\n"
                "            <string>" + c_uuid + "</string>\n"
                "            <key>PayloadVersion</key>\n            <integer>1</integer>\n"
                "        </dict>\n    </array>\n"
                "    <key>PayloadDisplayName</key>\n"
                "    <string>PocketBurp CA Profile</string>\n"
                "    <key>PayloadIdentifier</key>\n"
                "    <string>com.shashank.pocketburp.profile</string>\n"
                "    <key>PayloadRemovalDisallowed</key>\n    <false/>\n"
                "    <key>PayloadType</key>\n    <string>Configuration</string>\n"
                "    <key>PayloadUUID</key>\n"
                "    <string>" + p_uuid + "</string>\n"
                "    <key>PayloadVersion</key>\n    <integer>1</integer>\n"
                "</dict>\n</plist>"
            ).encode("utf-8")
            return self._send_bytes(
                mobileconfig, "application/x-apple-aspen-config",
                extra={"Content-Disposition": 'inline; filename="pocketburp.mobileconfig"'})
        if path == "/api/ca/cert.crt" and method == "GET":
            ca.ensure_ca()
            with open(ca.CA_CRT, "rb") as fh:
                data = fh.read()
            return self._send_bytes(
                data, "application/x-x509-ca-cert",
                extra={"Content-Disposition": 'attachment; filename="pocketburp-ca.crt"'})


        return self._err("unknown endpoint %s" % path, 404)


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass


def create_server(engine, host="127.0.0.1", port=9090):
    """Build (but do not start) the GUI/API server."""
    server = GuiServer((host, int(port)), ApiHandler)
    server.engine = engine
    return server
