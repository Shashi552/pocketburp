#!/bin/sh
# ============================================================================
#  PocketBurp -- one-command launcher for iSH Shell (Alpine Linux) on iPhone
#
#    sh run.sh                 # proxy :8080 + GUI :9090
#    sh run.sh --demo          # start with sample traffic in the history
#    sh run.sh --proxy-port 8888
#    sh run.sh --no-proxy      # GUI only
#
#  Environment overrides:  POCKETBURP_GUI_PORT, POCKETBURP_PROXY_PORT
# ============================================================================
set -e

cd "$(dirname "$0")" 2>/dev/null || cd .

GUI_PORT="${POCKETBURP_GUI_PORT:-9090}"
PROXY_PORT="${POCKETBURP_PROXY_PORT:-8080}"

say() { printf '%s\n' "$*"; }
die() { printf '\n  [!] %s\n\n' "$*"; exit 1; }

# ---------------------------------------------------------------- prerequisites
command -v python3 >/dev/null 2>&1 || die "python3 not found. Run: apk add python3 py3-pip openssl"
command -v openssl >/dev/null 2>&1 || die "openssl not found. Run: apk add openssl"

python3 - <<'PY' || die "Python 3.7 or newer is required."
import sys
raise SystemExit(0 if sys.version_info >= (3, 7) else 1)
PY

# ------------------------------------------------------------------- workspace
mkdir -p db certs/hosts static

# Free the ports if a previous run left something behind (best effort).
for port in "$GUI_PORT" "$PROXY_PORT"; do
  if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$port "; then
    say "  [i] port $port already in use -- if PocketBurp is already running, just open the GUI."
  fi
done

say ""
say "  Starting PocketBurp..."
say ""

exec python3 pocketburp.py --port "$GUI_PORT" --proxy-port "$PROXY_PORT" "$@"
