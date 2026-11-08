#!/usr/bin/env python3
"""
PocketBurp -- ca.py
Generates the PocketBurp Root CA and signs per-host leaf certificates.

Deliberately implemented on top of the `openssl` CLI (already present on
iOS / iSH Alpine after `apk add openssl`) instead of the `cryptography`
package, because `cryptography` is a large native extension that is painful
to install on iSH's ARM emulation. Zero Python dependencies.
"""

import ipaddress
import os
import re
import shutil
import ssl
import subprocess
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CERT_DIR = os.path.join(BASE, "certs")
HOST_DIR = os.path.join(CERT_DIR, "hosts")
CA_KEY = os.path.join(CERT_DIR, "pocketburp-ca.key")
CA_CRT = os.path.join(CERT_DIR, "pocketburp-ca.crt")
CA_SRL = os.path.join(CERT_DIR, "pocketburp-ca.srl")

OPENSSL = shutil.which("openssl") or "openssl"

# Apple requires server certs to be valid for no more than 398 days
LEAF_DAYS = 397
CA_DAYS = 3650

_lock = threading.RLock()
_ctx_cache = {}


class CAError(RuntimeError):
    pass


def _run(args, timeout=60):
    try:
        proc = subprocess.run(
            [OPENSSL] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise CAError(
            "openssl binary not found. Install it first: apk add openssl"
        )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise CAError("openssl %s failed: %s" % (args[0], err))
    return proc.stdout.decode("utf-8", "replace")


def _safe_name(host):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", host)[:120] or "host"


def ensure_ca():
    """Create the root CA if it does not exist yet. Idempotent."""
    os.makedirs(HOST_DIR, exist_ok=True)
    with _lock:
        if os.path.exists(CA_KEY) and os.path.exists(CA_CRT):
            return CA_CRT, CA_KEY
        cnf_path = os.path.join(CERT_DIR, "ca_gen.cnf")
        with open(cnf_path, "w") as fh:
            fh.write(
                "[req]\n"
                "distinguished_name = req_distinguished_name\n"
                "x509_extensions = v3_ca\n"
                "prompt = no\n\n"
                "[req_distinguished_name]\n"
                "C = IN\n"
                "O = PocketBurp\n"
                "OU = Security Research\n"
                "CN = PocketBurp Root CA\n\n"
                "[v3_ca]\n"
                "basicConstraints = critical,CA:TRUE,pathlen:0\n"
                "keyUsage = critical,keyCertSign,cRLSign,digitalSignature\n"
                "subjectKeyIdentifier = hash\n"
                "authorityKeyIdentifier = keyid:always,issuer\n"
            )
        try:
            try:
                _run([
                    "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
                    "-days", str(CA_DAYS),
                    "-keyout", CA_KEY,
                    "-out", CA_CRT,
                    "-config", cnf_path,
                ])
            except CAError:
                # Fallback for OpenSSL 1.1.1+ if -config fails
                _run([
                    "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
                    "-days", str(CA_DAYS),
                    "-keyout", CA_KEY,
                    "-out", CA_CRT,
                    "-subj", "/C=IN/O=PocketBurp/OU=Security Research/CN=PocketBurp Root CA",
                    "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign,digitalSignature",
                ])
        finally:
            try:
                os.remove(cnf_path)
            except OSError:
                pass
        try:
            os.chmod(CA_KEY, 0o600)
        except OSError:
            pass
        return CA_CRT, CA_KEY


def _leaf_is_usable(crt_path):
    try:
        out = _run(["x509", "-checkend", str(7 * 24 * 3600), "-noout", "-in", crt_path])
        return "Certificate will not expire" in out
    except CAError:
        return False


def _make_ext_file(host, path):
    try:
        ipaddress.ip_address(host)
        san = "IP:%s" % host
    except ValueError:
        san = "DNS:%s" % host
    with open(path, "w") as fh:
        fh.write(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=%s\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n" % san
        )


def cert_for_host(host):
    """Return (cert_path, key_path) for `host`, generating a signed leaf cert."""
    host = (host or "").strip().lower().strip(".")
    if not host:
        raise CAError("empty host")
    ensure_ca()
    with _lock:
        crt = os.path.join(HOST_DIR, _safe_name(host) + ".crt")
        key = os.path.join(CERT_DIR, "leaf.key")
        if not os.path.exists(key):
            _run(["genrsa", "-out", key, "2048"])
        if os.path.exists(crt) and _leaf_is_usable(crt):
            return crt, key

        csr = os.path.join(HOST_DIR, _safe_name(host) + ".csr")
        ext = os.path.join(HOST_DIR, _safe_name(host) + ".ext")
        _run([
            "req", "-new", "-sha256", "-key", key, "-out", csr,
            "-subj", "/C=IN/O=PocketBurp/CN=%s" % host,
        ])
        _make_ext_file(host, ext)
        _run([
            "x509", "-req", "-sha256", "-in", csr,
            "-CA", CA_CRT, "-CAkey", CA_KEY,
            "-CAserial", CA_SRL, "-CAcreateserial",
            "-days", str(LEAF_DAYS),
            "-extfile", ext,
            "-out", crt,
        ])
        for junk in (csr, ext):
            try:
                os.unlink(junk)
            except OSError:
                pass
        return crt, key


def ssl_context_for_host(host):
    """Server-side SSLContext presenting a forged certificate for `host`."""
    with _lock:
        ctx = _ctx_cache.get(host)
        if ctx is not None:
            return ctx
    crt, key = cert_for_host(host)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ssl.SSLError):
        pass
    ctx.set_alpn_protocols(["http/1.1"])
    ctx.load_cert_chain(crt, key)
    with _lock:
        _ctx_cache[host] = ctx
    return ctx


def ca_info():
    """Human readable details about the root CA."""
    ensure_ca()
    subject = issuer = not_after = fingerprint = serial = ""
    try:
        subject = _run(["x509", "-noout", "-subject", "-in", CA_CRT]).strip()
        subject = subject.split("=", 1)[1].strip() if "=" in subject else subject
    except CAError:
        pass
    try:
        issuer = _run(["x509", "-noout", "-issuer", "-in", CA_CRT]).strip()
        issuer = issuer.split("=", 1)[1].strip() if "=" in issuer else issuer
    except CAError:
        pass
    try:
        not_after = _run(["x509", "-noout", "-enddate", "-in", CA_CRT]).strip()
        not_after = not_after.split("=", 1)[1].strip() if "=" in not_after else not_after
    except CAError:
        pass
    try:
        fingerprint = _run(["x509", "-noout", "-fingerprint", "-sha256", "-in", CA_CRT]).strip()
        fingerprint = fingerprint.split("=", 1)[1].strip() if "=" in fingerprint else fingerprint
    except CAError:
        pass
    try:
        serial = _run(["x509", "-noout", "-serial", "-in", CA_CRT]).strip()
        serial = serial.split("=", 1)[1].strip() if "=" in serial else serial
    except CAError:
        pass
    return {
        "name": "PocketBurp Root CA",
        "subject": subject,
        "issuer": issuer,
        "expires": not_after,
        "fingerprint": fingerprint,
        "serial": serial,
        "path": CA_CRT,
        "pem": _read_text(CA_CRT),
        "leaf_days": LEAF_DAYS,
        "generated": os.path.exists(CA_CRT),
    }


def _read_text(path):
    try:
        with open(path, "r") as fh:
            return fh.read()
    except OSError:
        return ""


if __name__ == "__main__":
    print(ensure_ca())
    info = ca_info()
    for k in ("subject", "issuer", "expires", "fingerprint"):
        print("%-12s %s" % (k + ":", info[k]))
