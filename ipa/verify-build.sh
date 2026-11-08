#!/bin/bash
# =============================================================================
#  PocketBurp -- ipa/verify-build.sh
#
#  Run this on your Mac after ./build-on-mac.sh and paste the whole output back.
#  It collects exactly what is needed to diagnose a build without any remote
#  access, shell session, or screen sharing.
#
#    cd ipa && ./verify-build.sh > ~/Desktop/pocketburp-report.txt 2>&1
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")" || exit 1

line() { printf '%s\n' "------------------------------------------------------------"; }
sec()  { line; printf '%s\n' "$*"; line; }

sec "ENVIRONMENT"
printf 'date      : %s\n' "$(date)"
printf 'uname     : %s\n' "$(uname -a)"
printf 'macOS     : %s\n' "$(sw_vers -productVersion 2>/dev/null || echo n/a)"
if command -v xcodebuild >/dev/null 2>&1; then
  printf 'xcode     : %s\n' "$(xcodebuild -version 2>/dev/null | tr '\n' ' ')"
  printf 'devdir    : %s\n' "$(xcode-select -p 2>/dev/null)"
  printf 'iOS SDK   : %s\n' "$(xcrun --sdk iphoneos --show-sdk-version 2>/dev/null || echo none)"
else
  printf 'xcode     : NOT INSTALLED\n'
fi
command -v python3 >/dev/null && printf 'python3   : %s\n' "$(python3 -V 2>&1)"

sec "PROJECT"
if command -v python3 >/dev/null 2>&1; then
  python3 tools/check_pbxproj.py 2>&1
else
  printf 'python3 missing, skipped structural check\n'
fi
if command -v xcodebuild >/dev/null 2>&1; then
  printf '\nxcodebuild -list:\n'
  xcodebuild -list -project PocketBurp.xcodeproj 2>&1 | sed 's/^/  /'
fi

sec "STAGED ENGINE"
if [ -d PocketBurp/pocketburp ]; then
  printf 'files: %s\n' "$(find PocketBurp/pocketburp -type f | wc -l | tr -d ' ')"
  find PocketBurp/pocketburp -maxdepth 2 -type f | sed 's/^/  /' | head -30
else
  printf 'NOT STAGED (run ./build-on-mac.sh first)\n'
fi

sec "IPA"
IPA="build/PocketBurp.ipa"
if [ -f "$IPA" ]; then
  printf 'path : %s\nsize : %s\n' "$IPA" "$(du -h "$IPA" | cut -f1)"
  printf 'sha256: %s\n' "$(shasum -a 256 "$IPA" | cut -d' ' -f1)"
  printf '\ncontents (top level):\n'
  unzip -l "$IPA" | awk 'NR>3 && NF>=4 {print "  "$4}' | cut -d/ -f1-2 | sort -u | head -25
  rm -rf /tmp/pb_verify && mkdir -p /tmp/pb_verify
  unzip -q -o "$IPA" -d /tmp/pb_verify
  APP="/tmp/pb_verify/Payload/PocketBurp.app"
  if [ -d "$APP" ]; then
    printf '\nInfo.plist:\n'
    for k in CFBundleIdentifier CFBundleShortVersionString CFBundleVersion \
             MinimumOSVersion CFBundleExecutable UIDeviceFamily; do
      v=$(/usr/libexec/PlistBuddy -c "Print :$k" "$APP/Info.plist" 2>/dev/null)
      printf '  %-24s %s\n' "$k" "$v"
    done
    printf '\narchitecture:\n  %s\n' "$(lipo -info "$APP/PocketBurp" 2>/dev/null | sed 's/^.*are: //')"
    printf '\ncode signature:\n'
    codesign -dv "$APP" 2>&1 | sed 's/^/  /' | head -12
    printf '\nengine inside the app:\n'
    [ -d "$APP/pocketburp" ] && find "$APP/pocketburp" -type f | wc -l | sed 's/^/  files: /' \
      || printf '  MISSING -- folder reference did not copy\n'
    printf '\nassets:\n'
    ls "$APP" | sed 's/^/  /'
  else
    printf 'Payload/PocketBurp.app NOT FOUND inside the ipa\n'
  fi
  rm -rf /tmp/pb_verify
else
  printf 'no build/PocketBurp.ipa yet\n'
fi

sec "BUILD LOG TAIL"
if [ -f build/build.log ]; then
  grep -E "error:|warning: .*sign|BUILD|ARCHIVE" build/build.log | tail -25 | sed 's/^/  /'
else
  printf 'no build/build.log\n'
fi

sec "END"
printf 'Paste the text above into the chat. It contains no secrets:\n'
printf 'no Apple ID password, no provisioning profile contents, no device tokens.\n'
