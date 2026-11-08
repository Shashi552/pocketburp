# PocketBurp — agent rules

iPhone-only Burp Suite CE replica: a pure-stdlib Python proxy engine plus a
single-file web GUI. This repo is **complete and verified**. The only remaining
task is turning it into a signed `PocketBurp.ipa` on macOS.

**Pick the right brief first — check the OS:**

```bash
sw_vers -productVersion
```

- **macOS 13 Ventura or newer** → read **`ipa/ANTIGRAVITY-PROMPT.md`** and follow
  it. Local Xcode build. Ground truth, steps, gates, scenarios, error playbook.
- **macOS 12 Monterey or older, or no Xcode.app** → local Xcode is impossible
  (Xcode 15+ needs macOS 13+; the ceiling here is Xcode 14.2). Read
  **`ipa/ANTIGRAVITY-CI-PROMPT.md`** instead: build on a GitHub-hosted
  `macos-15` runner, download the artifact, verify it. Do not try to install
  Xcode locally.

Do not improvise a plan outside the chosen brief.

## Hard rules

- **Do not rewrite the engine.** `proxy.py`, `api.py`, `ca.py`, `history.py`,
  `scope.py`, `intercept.py`, `repeater.py`, `scanner.py`, `pocketburp.py`,
  `run.sh`, `config.json` are frozen. Their behaviour and REST contract are
  verified by a 15-check regression that must stay green.
- **No new dependencies.** No SPM, CocoaPods, Carthage, npm, pip installs. The
  iOS app is UIKit + WebKit from the system SDK only. Nothing may be downloaded
  at build time.
- **Do not rename** the target, scheme, bundle id (`com.shashank.pocketburp`) or
  the `PocketBurp/pocketburp/` folder reference — the build script and
  `EngineBridge` depend on all of them.
- **Do not add `UIApplicationSceneManifest`.** The app uses `AppDelegate` + a
  manual `UIWindow`. A scene manifest without a `SceneDelegate` = black screen.
- **Never fake an `.ipa`.** It must come from `xcodebuild archive` plus a real
  `Payload/` zip. Zipping a `.app`, renaming, or reusing a cached build is fraud.
- **Never print secrets**: no Apple ID password, no profile contents, no UDIDs
  beyond what `devicectl` shows locally.

## Physical constraint you must not design around

A `WKWebView` cannot `bind()` a TCP port. The Swift shell is the window; the
Python engine runs in **iSH Shell** on the device and owns 8080/9090. If the
engine is down the app shows a recovery card — that is correct behaviour, not a
bug. Never simulate a server inside the app bundle.

## Build commands

Local (macOS 13+ with Xcode):

```bash
cd ipa && ./build-on-mac.sh --doctor            # toolchain + identities, builds nothing
cd ipa && ./build-on-mac.sh --team <TEAMID>     # signed build -> build/PocketBurp.ipa
cd ipa && ./build-on-mac.sh                     # unsigned build (LiveContainer/Sideloadly)
cd ipa && ./verify-build.sh                     # diagnostic report on failure
```

Cloud (works from any OS, no Xcode):

```bash
gh workflow run build-ipa.yml && gh run watch   # runner: macos-15 ONLY
gh run download <run-id> -n PocketBurp-ipa -D ~/Desktop
```

`python3 ipa/tools/check_pbxproj.py -v` runs anywhere and must print `RESULT: OK`.

> **Never set `runs-on: macos-14`.** GitHub retired that runner image on
> 2026-11-02; jobs targeting it fail instantly. Use `macos-15`.

## Non-negotiable gates before reporting success

- `ipa/build/PocketBurp.ipa` exists and is > 1 MB.
- Inside the bundle: `pocketburp/pocketburp.py` **and** `pocketburp/static/index.html`.
- `lipo -info` says `arm64`; `CFBundleIdentifier` = `com.shashank.pocketburp`;
  `MinimumOSVersion` = `15.0`.
- `python3 ipa/tools/check_pbxproj.py -v` → `RESULT: OK`.
- `git status --porcelain` shows no modified engine file.

## Stop and ask instead of guessing when

- several signing identities exist and none matches `MPZ586FDM5`;
- the same build error repeats after the playbook fix;
- a fix would need an engine edit, a dependency, a network fetch, or an entitlement;
- you are about to report success without having run every gate above.

Report using the template in `ipa/ANTIGRAVITY-PROMPT.md` §9.
