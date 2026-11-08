#!/usr/bin/env python3
"""
PocketBurp -- pocketburp.py
Main entry point. Starts the intercepting proxy thread (port 8080) and the
web GUI / REST API server (port 9090) inside one Python process, so a single
command in iSH brings the whole tool up.

    python3 pocketburp.py                 # normal use on the iPhone
    python3 pocketburp.py --demo          # UI preview with sample traffic
    python3 pocketburp.py --host 0.0.0.0  # expose GUI on the LAN (careful)
"""

import argparse
import os
import signal
import sys
import threading
import time

import api
import ca
import history
import intercept
import proxy
import repeater
import scanner
import scope

VERSION = "1.0"
BASE = os.path.dirname(os.path.abspath(__file__))
BANNER = r"""
  ___         _        _   ___            _
 | _ \___  __| |_____ | |_| _ )_  _ _ _ (_)_ _
 |  _/ _ \/ _| / -_) '|  _| _ \ || | '_|| | '_|
 |_| \___/\__|_\___||_|\__|___/\_,_|_|  |_|_|
 PocketBurp %s   |   intercept. edit. replay. fuzz.
""" % VERSION


def ensure_dirs():
    for d in ("db", "certs", os.path.join("certs", "hosts"), "static"):
        os.makedirs(os.path.join(BASE, d), exist_ok=True)


def seed_demo():
    """Fill the GUI with sample exchanges so the interface is explorable
    before any traffic has been captured. Rows are tagged source='demo'."""
    if history.count() > 0:
        return 0
    samples = [
        dict(method="GET", host="192.168.1.42", port=3000, scheme="http",
             path="/files?path=docs/readme.txt", status=200, time_ms=41,
             req_headers="Host: 192.168.1.42:3000\r\nUser-Agent: Mozilla/5.0 (iPhone)\r\nAccept: */*",
             resp_headers="HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nServer: web-file-server",
             resp_body=b"# Sornaram web file server\nreads files relative to ./public\n",
             note="demo"),
        dict(method="GET", host="192.168.1.42", port=3000, scheme="http",
             path="/files?path=../../../../etc/passwd", status=200, time_ms=53,
             req_headers="Host: 192.168.1.42:3000\r\nUser-Agent: Mozilla/5.0 (iPhone)",
             resp_headers="HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream",
             resp_body=b"root:x:0:0:root:/root:/bin/sh\nbin:x:1:1:bin:/bin:/sbin/nologin\n",
             note="demo | traversal probe"),
        dict(method="POST", host="shop.example.test", port=443, scheme="https",
             path="/wp-admin/admin-ajax.php", status=200, time_ms=118,
             req_headers=("Host: shop.example.test\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                          "User-Agent: Mozilla/5.0 (iPhone)"),
             req_body=b"action=lc_save&post_id=41&title=Hello",
             resp_headers="HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=UTF-8",
             resp_body=b'<div class="lc-saved" data-title="Hello">Saved</div>',
             note="demo | Live Composer inject"),
        dict(method="POST", host="shop.example.test", port=443, scheme="https",
             path="/wp-admin/admin-ajax.php", status=200, time_ms=96,
             req_headers=("Host: shop.example.test\r\nContent-Type: application/x-www-form-urlencoded"),
             req_body=b'action=lc_save&post_id=41&title=<script>alert(document.domain)</script>',
             resp_headers="HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=UTF-8",
             resp_body=b'<div class="lc-saved" data-title="<script>alert(document.domain)</script>">Saved</div>',
             note="demo | reflected payload"),
        dict(method="POST", host="store.example.test", port=443, scheme="https",
             path="/?edd_action=verify_payment", status=302, time_ms=204,
             req_headers=("Host: store.example.test\r\nContent-Type: application/x-www-form-urlencoded\r\n"
                          "Cookie: edd_session=abc123"),
             req_body=b"payment_id=8812&status=complete&amount=0.00",
             resp_headers="HTTP/1.1 302 Found\r\nLocation: /purchase-confirmation/?payment=8812",
             resp_body=b"", note="demo | EDD payment flow"),
        dict(method="GET", host="api.example.test", port=443, scheme="https",
             path="/v1/user/1234", status=500, time_ms=812,
             req_headers="Host: api.example.test\r\nAuthorization: Bearer eyJhbGciOi...",
             resp_headers="HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json",
             resp_body=b'{"error":"unhandled rejection: Cannot read properties of null"}',
             note="demo"),
        dict(method="GET", host="cdn.example.test", port=443, scheme="https",
             path="/static/app.bundle.js", status=304, time_ms=12,
             req_headers="Host: cdn.example.test\r\nIf-None-Match: \"9f2-ac\"",
             resp_headers="HTTP/1.1 304 Not Modified\r\nETag: \"9f2-ac\"",
             resp_body=b"", note="demo"),
    ]
    for s in samples:
        history.log(source="demo", tls=s["scheme"] == "https", method=s["method"],
                    host=s["host"], port=s["port"], scheme=s["scheme"],
                    path=s["path"], req_headers=s["req_headers"],
                    req_body=s.get("req_body", b""), status=s["status"],
                    resp_headers=s["resp_headers"], resp_body=s["resp_body"],
                    time_ms=s["time_ms"], note=s["note"])
    scanner.seed_demo()
    return len(samples)


def _start_ish_keepalive():
    """On iOS (iSH Shell), reading /dev/location keeps iOS CoreLocation active,
    preventing iOS from freezing or suspending the process when backgrounded.
    Sleeping 5s between reads avoids 100% CPU usage and prevents battery drain."""
    if os.path.exists("/dev/location"):
        def _loop():
            try:
                with open("/dev/location", "rb") as fh:
                    while True:
                        data = fh.read(64)
                        if not data:
                            break
                        time.sleep(5)
            except Exception:
                pass
        t = threading.Thread(target=_loop, name="ish-keepalive", daemon=True)
        t.start()


def main(argv=None):
    _start_ish_keepalive()
    parser = argparse.ArgumentParser(description="PocketBurp -- mobile Burp Suite replica")
    parser.add_argument("--host", default="127.0.0.1",
                        help="GUI bind address (default 127.0.0.1 = this device only)")
    parser.add_argument("--port", type=int, default=9090, help="GUI port (default 9090)")
    parser.add_argument("--proxy-host", default="0.0.0.0",
                        help="proxy bind address (default 0.0.0.0)")
    parser.add_argument("--proxy-port", type=int, default=8080,
                        help="proxy port (default 8080)")
    parser.add_argument("--no-proxy", action="store_true",
                        help="start only the GUI (proxy stays stopped)")
    parser.add_argument("--demo", action="store_true",
                        help="seed sample traffic into history for a UI preview")
    parser.add_argument("--reset", action="store_true", help="wipe history on start")
    args = parser.parse_args(argv)

    ensure_dirs()
    history.init()
    if args.reset:
        history.clear()
    try:
        ca.ensure_ca()
        ca_info = ca.ca_info()
        ca_status = "ready (%s)" % ca_info["fingerprint"][:23]
    except ca.CAError as exc:
        ca_status = "ERROR: %s" % exc

    if args.demo:
        n = seed_demo()
        if n:
            print("  demo: seeded %d sample exchanges" % n)

    engine = proxy.ProxyEngine(args.proxy_host, args.proxy_port)
    proxy_msg = "not started (--no-proxy)"
    if not args.no_proxy:
        ok, proxy_msg = engine.start()
        if not ok:
            print("  [!] proxy did not start: %s" % proxy_msg)

    try:
        server = api.create_server(engine, args.host, args.port)
    except OSError as exc:
        print("  [!] cannot bind GUI on %s:%d -- %s" % (args.host, args.port, exc))
        return 1

    thread = threading.Thread(target=server.serve_forever, name="pocketburp-gui",
                              daemon=True)
    thread.start()

    print(BANNER)
    print("  proxy      : %s:%d  (%s)" % (args.proxy_host, args.proxy_port, proxy_msg))
    print("  GUI        : http://%s:%d" % (
        "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host, args.port))
    print("  CA cert    : %s" % ca_status)
    print("  intercept  : %s" % ("ON (holding traffic)" if intercept.QUEUE.enabled else "OFF"))
    print("  history db : %s" % history.DB_PATH)
    print("\n  iPhone Wi-Fi proxy -> Manual -> Server 127.0.0.1  Port %d" % args.proxy_port)
    print("  Then install the CA from the 'CA Certificate' tab.  Ctrl-C to quit.\n")

    stopping = {"flag": False}

    def shutdown(signum=None, frame=None):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        print("\n  shutting down...")
        try:
            engine.stop()
        except Exception:
            pass
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    try:
        signal.signal(signal.SIGTERM, shutdown)
    except Exception:
        pass

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    sys.exit(main() or 0)
