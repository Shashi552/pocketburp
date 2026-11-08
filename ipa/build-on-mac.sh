#!/bin/bash
# =============================================================================
#  PocketBurp -- build (and optionally install) the iOS app on YOUR Mac
#
#  Everything runs locally. Nothing is sent anywhere, and no remote shell,
#  tunnel or public link is needed or wanted.
#
#  Quick start
#  -----------
#    cd ipa
#    ./build-on-mac.sh                 # unsigned .ipa  (for LiveContainer)
#    ./build-on-mac.sh --team MPZ586FDM5   # signed with your Apple ID cert
#    ./build-on-mac.sh --install       # signed build + push to a connected iPhone
#    ./build-on-mac.sh --doctor        # just check the toolchain, change nothing
#
#  Output: build/PocketBurp.ipa
# =============================================================================
set -uo pipefail

cd "$(dirname "$0")" || exit 1

SCHEME="PocketBurp"
PROJECT="PocketBurp.xcodeproj"
APP_NAME="PocketBurp"
OUT="build"
ARCHIVE="$OUT/$APP_NAME.xcarchive"
IPA="$OUT/$APP_NAME.ipa"
ENGINE_DIR="$(cd .. && pwd)"

TEAM=""
DO_INSTALL=0
DO_DOCTOR=0
CLEAN=0
DEVICE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --team)     TEAM="${2:-}"; shift 2 ;;
    --install)  DO_INSTALL=1; shift ;;
    --doctor)   DO_DOCTOR=1; shift ;;
    --clean)    CLEAN=1; shift ;;
    --device)   DEVICE="${2:-}"; shift 2 ;;
    -h|--help)  sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)"; exit 2 ;;
  esac
done

# ------------------------------------------------------------------ helpers
step() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n   \033[1;31m✗ %s\033[0m\n\n' "$*"; exit 1; }

# ------------------------------------------------------------------ doctor
step "Toolchain check"
[ "$(uname -s)" = "Darwin" ] || die "This script must run on macOS (uname says $(uname -s))."

if ! xcode-select -p >/dev/null 2>&1; then
  die "Xcode command line tools not selected.
      Install Xcode from the App Store, then run:
        sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
fi
ok "developer dir: $(xcode-select -p)"

if ! command -v xcodebuild >/dev/null 2>&1; then
  die "xcodebuild missing -- install full Xcode (not just Command Line Tools)."
fi
XCODE_VER="$(xcodebuild -version 2>/dev/null | head -1)"
ok "$XCODE_VER"

SDK_VER="$(xcrun --sdk iphoneos --show-sdk-version 2>/dev/null)"
if [ -n "$SDK_VER" ]; then ok "iOS SDK $SDK_VER"; else
  die "No iOS SDK. Open Xcode once and let it finish installing components."
fi

if xcodebuild -checkFirstLaunchStatus >/dev/null 2>&1; then :; else
  warn "first-launch tasks pending; running 'xcodebuild -runFirstLaunch' may prompt for sudo"
fi

command -v python3 >/dev/null 2>&1 && ok "python3 $(python3 -V 2>&1 | awk '{print $2}')" \
  || warn "python3 not found (needed to copy the engine + run the project checker)"

# signing identities decide whether you get a signed .ipa or an unsigned one
IDS="$(security find-identity -v -p codesigning 2>/dev/null | sed -n 's/^ *[0-9][0-9]*) \(.*\)$/\1/p')"
if [ -n "$IDS" ]; then
  ok "codesigning identities:"
  printf '%s\n' "$IDS" | sed 's/^/       /'
else
  warn "no codesigning identity found -- build unsigned, then sign at install time"
fi

step "Project integrity"
if python3 tools/check_pbxproj.py >/tmp/pb_check.log 2>&1; then
  ok "project.pbxproj parses, no dangling references"
else
  cat /tmp/pb_check.log
  die "project file is inconsistent -- fix before building."
fi

if [ "$DO_DOCTOR" = "1" ]; then
  step "Devices"
  xcrun devicectl list devices 2>/dev/null | sed 's/^/   /' || warn "devicectl unavailable (needs Xcode 15+)"
  printf '\n   Doctor done. Nothing was built or changed.\n\n'
  exit 0
fi

# ------------------------------------------------------------------ stage engine
step "1/5  staging the Python engine + GUI into the app bundle"
rm -rf "$APP_NAME/pocketburp"
mkdir -p "$APP_NAME/pocketburp/static"
for f in ca.py history.py scope.py intercept.py proxy.py repeater.py scanner.py \
         api.py pocketburp.py run.sh README.md config.json; do
  [ -f "$ENGINE_DIR/$f" ] && cp "$ENGINE_DIR/$f" "$APP_NAME/pocketburp/" \
    || warn "missing $f (skipped)"
done
# every static asset: index.html, manifest, service worker, icon set
cp -R "$ENGINE_DIR/static/." "$APP_NAME/pocketburp/static/" 2>/dev/null \
  || die "cannot copy static/ -- run this from the ipa/ folder inside the project"
ok "$(find "$APP_NAME/pocketburp" -type f | wc -l | tr -d ' ') files staged"

# ------------------------------------------------------------------ build
step "2/5  building (Release, iphoneos arm64)"
[ "$CLEAN" = "1" ] && rm -rf "$OUT" && ok "clean"

SIGN_ARGS=(CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="")
if [ -n "$TEAM" ]; then
  SIGN_ARGS=(DEVELOPMENT_TEAM="$TEAM" CODE_SIGN_STYLE=Automatic)
  ok "signing with team $TEAM (Xcode may ask once for your Apple ID)"
else
  ok "unsigned build (LiveContainer / TrollStore ready)"
fi

mkdir -p "$OUT"
set -o pipefail
xcodebuild \
  -project "$PROJECT" \
  -scheme "$SCHEME" \
  -configuration Release \
  -sdk iphoneos \
  -derivedDataPath "$OUT/dd" \
  -archivePath "$ARCHIVE" \
  "${SIGN_ARGS[@]}" \
  archive 2>&1 | tee "$OUT/build.log" | grep -E "error:|warning: .*(signing|provision)|BUILD SUCCEEDED|BUILD FAILED|\*\*" || true

if ! grep -q "ARCHIVE SUCCEEDED\|BUILD SUCCEEDED" "$OUT/build.log"; then
  printf '\n'
  grep -E "error:" "$OUT/build.log" | head -20 | sed 's/^/   /'
  die "Build failed. Full log: $OUT/build.log"
fi
ok "archive built"

# ------------------------------------------------------------------ package
step "3/5  packaging the .ipa"
APP="$ARCHIVE/Products/Applications/$APP_NAME.app"
[ -d "$APP" ] || die "cannot find $APP in the archive"
rm -rf "$OUT/Payload" "$IPA"
mkdir -p "$OUT/Payload"
cp -R "$APP" "$OUT/Payload/"
(cd "$OUT" && zip -qry "$APP_NAME.ipa" Payload)
rm -rf "$OUT/Payload"
ok "$IPA ($(du -h "$IPA" | cut -f1))"

# ------------------------------------------------------------------ verify
step "4/5  verifying the bundle"
BIN="$APP/$APP_NAME"
[ -f "$BIN" ] || die "no executable inside the .app"
ok "arch: $(lipo -info "$BIN" 2>/dev/null | sed 's/.*are: //')"
ok "min iOS: $(/usr/libexec/PlistBuddy -c 'Print :MinimumOSVersion' "$APP/Info.plist" 2>/dev/null || echo 'from SDK default')"
ok "bundle id: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Info.plist")"
[ -d "$APP/pocketburp" ] && ok "engine bundled: $(find "$APP/pocketburp" -type f | wc -l | tr -d ' ') files" \
  || warn "engine folder not present in the .app"
if codesign -dv "$APP" >/dev/null 2>&1; then
  ok "signed: $(codesign -dv "$APP" 2>&1 | grep -m1 'Authority=' | cut -d= -f2-)"
else
  ok "unsigned (expected; LiveContainer/TrollStore or a re-sign tool handles it)"
fi

# ------------------------------------------------------------------ install
if [ "$DO_INSTALL" = "1" ]; then
  step "5/5  installing to a connected iPhone"
  if [ -z "$TEAM" ]; then
    warn "--install needs a signed build; re-run with --team YOUR_TEAM_ID"
  elif ! xcrun devicectl help >/dev/null 2>&1; then
    warn "devicectl missing (needs Xcode 15+). Drag $IPA onto Xcode's device window instead."
  else
    if [ -n "$DEVICE" ]; then
      xcrun devicectl device install app --device "$DEVICE" "$APP" && ok "installed"
    else
      printf '   connected devices:\n'
      xcrun devicectl list devices 2>/dev/null | tail -n +1 | sed 's/^/     /'
      printf '\n   re-run with:  ./build-on-mac.sh --team %s --install --device <UDID>\n' "$TEAM"
    fi
  fi
else
  step "5/5  next step"
  printf '   LiveContainer  : AirDrop / Files the .ipa to the iPhone, then\n'
  printf '                    LiveContainer -> "+" -> import -> PocketBurp.ipa -> Launch\n'
  printf '   Sideloadly     : drag %s into it (it re-signs with your Apple ID)\n' "$IPA"
  printf '   Xcode          : Window -> Devices and Simulators -> drag the .ipa\n'
fi

printf '\n\033[1;32m Done: %s\033[0m\n\n' "$IPA"
