#!/bin/bash
# =============================================================================
#  PocketBurp -- build a sideloadable .ipa  (run on macOS with Xcode installed,
#  or let .github/workflows/build-ipa.yml do it for you in the cloud)
#
#  Usage:
#    ./build-ipa.sh                     # unsigned .ipa (LiveContainer / TrollStore)
#    TEAM_ID=MPZ586FDM5 ./build-ipa.sh  # signed with your Apple ID certificate
#
#  Output: build/PocketBurp.ipa
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")"
OUT_DIR="build"
SCHEME="PocketBurp"
PROJECT="PocketBurp.xcodeproj"
ARCHIVE="$OUT_DIR/PocketBurp.xcarchive"
APP="$OUT_DIR/PocketBurp.app"
IPA="$OUT_DIR/PocketBurp.ipa"

mkdir -p "$OUT_DIR"
rm -rf "$APP" "$IPA"

if ! command -v xcodebuild >/dev/null 2>&1; then
  echo "  [!] xcodebuild not found -- this must run on macOS with Xcode." >&2
  echo "      On Linux you can still build it in CI: see .github/workflows/build-ipa.yml" >&2
  exit 1
fi

echo "== 1/4  copying the Python engine + GUI into the app bundle =="
# The web GUI and proxy engine ship as app resources so the embedded Alpine
# can run them from the app's Documents folder on first launch.
ENGINE_DIR="${POCKETBURP_ENGINE_DIR:-$(cd .. && pwd)}"
if [ ! -f "$ENGINE_DIR/proxy.py" ]; then
  echo "  ✗ no engine at $ENGINE_DIR (expected proxy.py there)." >&2
  echo "    Set POCKETBURP_ENGINE_DIR to the folder holding the .py files." >&2
  exit 1
fi

rm -rf "PocketBurp/pocketburp"          # never ship a stale or partial engine
mkdir -p "PocketBurp/pocketburp"

for f in ca.py history.py scope.py intercept.py proxy.py repeater.py scanner.py \
         api.py pocketburp.py run.sh README.md config.json; do
  [ -f "$ENGINE_DIR/$f" ] || { echo "  ✗ missing engine file: $f" >&2; exit 1; }
  cp "$ENGINE_DIR/$f" "PocketBurp/pocketburp/"
done

# EVERY static asset -- index.html, manifest, service worker, the whole icon set.
# Copying only index.html here silently shipped an app with no PWA manifest,
# no service worker and no icons; that regression is what this block prevents.
cp -R "$ENGINE_DIR/static/." "PocketBurp/pocketburp/static/" || {
  echo "  ✗ cannot copy static/ from $ENGINE_DIR" >&2; exit 1; }

STAGED=$(find "PocketBurp/pocketburp" -type f | wc -l | tr -d " ")
for required in pocketburp.py api.py static/index.html \
                static/manifest.webmanifest static/sw.js; do
  [ -f "PocketBurp/pocketburp/$required" ] || {
    echo "  ✗ staging incomplete: $required not staged" >&2; exit 1; }
done
if [ "$STAGED" -lt 20 ]; then
  echo "  ✗ only $STAGED files staged (expected 20+). Engine or assets missing." >&2
  exit 1
fi
echo "   staged $STAGED files"

echo "== 2/4  building (Release, device arm64) =="
SIGN_ARGS=(CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="")
if [ -n "${TEAM_ID:-}" ]; then
  SIGN_ARGS=(DEVELOPMENT_TEAM="$TEAM_ID" CODE_SIGN_STYLE=Automatic)
fi

xcodebuild \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -configuration Release \
  -sdk iphoneos \
  -derivedDataPath "$OUT_DIR/dd" \
  -archivePath "$ARCHIVE" \
  "${SIGN_ARGS[@]}" \
  archive

echo "== 3/4  extracting PocketBurp.app =="
cp -R "$ARCHIVE/Products/Applications/PocketBurp.app" "$APP"

echo "== 4/4  packaging .ipa =="
mkdir -p "$OUT_DIR/Payload"
rm -rf "$OUT_DIR/Payload/PocketBurp.app"
cp -R "$APP" "$OUT_DIR/Payload/"
(cd "$OUT_DIR" && zip -qry "PocketBurp.ipa" Payload)
rm -rf "$OUT_DIR/Payload"

echo
echo "  ✓ $IPA"
echo "    size : $(du -h "$IPA" | cut -f1)"
echo
echo "  Install on a NON-jailbroken iPhone:"
echo "    • LiveContainer  -> import PocketBurp.ipa -> launch (JIT-less OK)"
echo "    • Sideloadly / AltStore / TrollStore -> install with your Apple ID"
echo "    • Signed builds: Profiles & Device Management -> Trust your cert"
echo
if [ -z "${TEAM_ID:-}" ]; then
  echo "  NOTE: unsigned build. LiveContainer and TrollStore accept it;"
  echo "        AltStore/Sideloadly will re-sign it with your own certificate."
fi
