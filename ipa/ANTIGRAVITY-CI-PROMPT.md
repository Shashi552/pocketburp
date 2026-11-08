# POCKETBURP — BUILD THE .ipa IN THE CLOUD (macOS 12.7.6)

You are picking up a build task that already failed once, for a reason that is
**permanent and not your fault**. Read §1 before touching anything. Then follow
the steps in order. Do not redesign, refactor, or "improve" the app.

---

## 1. WHY THE LOCAL BUILD IS OFF THE TABLE — DO NOT RETRY IT

This Mac runs **macOS 12.7.6 (Monterey)** with only Command Line Tools.
That is not a missing install you can fix:

- Xcode **15 and later require macOS 13 Ventura or newer**. They will not install.
- The newest Xcode that runs on Monterey is **14.2**, which is no longer offered
  in the App Store for this OS and must be side-loaded from developer.apple.com
  as a ~7 GB `.xip`, needing ~35 GB of free disk and an Apple ID sign-in.
- Command Line Tools alone cannot produce an iOS binary — there is no `iphoneos`
  SDK, no `xcodebuild` archival, no signing infrastructure.

So: **do not** try `xcode-select --install`, do not try to fetch Xcode, do not
try to fake a `.ipa` by zipping a `.app`. If you catch yourself heading there,
stop and use §3.

The build happens on a **GitHub-hosted macOS 15 runner** instead. Everything is
already configured for that in the repo. Your job is to drive the round trip and
verify the artifact.

---

## 2. GROUND TRUTH — VERIFY, DO NOT REDISCOVER

| Fact | Value |
|---|---|
| Workspace root | the folder containing `proxy.py`, `api.py`, `static/`, `ipa/`, `AGENTS.md` |
| CI workflow | `.github/workflows/build-ipa.yml` |
| Required runner label | **`macos-15`** — see the warning below |
| Xcode project | `ipa/PocketBurp.xcodeproj` (objectVersion 60 — fine on Xcode 16/26) |
| Bundle id | `com.shashank.pocketburp` |
| Deployment target | iOS 15.0 |
| Staged engine file count | **24** (12 root files + 12 in `static/`) |
| Artifact name | `PocketBurp-ipa` → contains `PocketBurp.ipa` |
| Signing | unsigned (LiveContainer / Sideloadly sign at install time) |

### THE ZOMBIE RUNNER WARNING — read this twice

**GitHub retired the `macos-14` runner image on 2026-11-02.** Any workflow still
saying `runs-on: macos-14` fails immediately with *"This job failed because of a
scheduled macOS 14 runner image brownout"*. A previous attempt at this task used
`macos-14` and that is one more reason it could never have worked.

Check the file before you push:

```bash
grep -n "runs-on" .github/workflows/build-ipa.yml
```

- Expect exactly `runs-on: macos-15`. Good — continue.
- If it says `macos-14`, change that one line to `macos-15`. Do not restructure
  the workflow.
- If it says `macos-latest`, that currently resolves to `macos-26` and also
  works; leave it.

---

## 3. HARD CONSTRAINTS — DO NOT VIOLATE

1. **Do not modify the engine.** `proxy.py`, `api.py`, `ca.py`, `history.py`,
   `scope.py`, `intercept.py`, `repeater.py`, `scanner.py`, `pocketburp.py`,
   `run.sh`, `config.json`, `static/**` are frozen and verified.
2. **Do not modify `.github/workflows/build-ipa.yml`** beyond the single runner
   label correction in §2. It already stages the engine, runs the project
   validator, enforces the artifact gates and uploads both the `.ipa` and the
   build log.
3. **No new dependencies** anywhere. No SPM, CocoaPods, npm, pip, brew.
4. **Never print or ask for secrets** in chat: no GitHub token, no Apple ID
   password, no app-specific password. Browser-based auth only (§4 Step 2).
5. **Never fake an `.ipa`.** It must be the artifact produced by `xcodebuild`
   inside the runner. Zipping a `.app` directory, renaming a file, or reusing an
   older build is fraud, not engineering.
6. **Never claim success without running §6 gates** on the downloaded file.

---

## 4. PATH A — WITH THE GITHUB CLI (preferred)

### Step 1 — Check tooling

```bash
git --version
command -v gh && gh --version || echo "gh NOT INSTALLED"
```

`git` is present because Command Line Tools is installed. If `gh` is missing:

> **Do not use Homebrew on Monterey** — Homebrew no longer ships bottles for
> macOS 12 and will try to build from source or refuse. Download the official
> installer instead:
> https://github.com/cli/cli/releases/latest
> Take `gh_*_macOS_amd64.pkg` (Intel) or `gh_*_macOS_arm64.pkg` (Apple Silicon);
> check with `uname -m` → `x86_64` = amd64, `arm64` = arm64. Install with
> `sudo installer -pkg <file>.pkg -target /`.
>
> If the user prefers not to install anything, skip to **Path B** in §5.

### Step 2 — Authenticate (the user does this, not you)

```bash
gh auth login
```

Choose: **GitHub.com** → **HTTPS** → **Login with a web browser**.
`gh` prints a one-time code and opens a browser.

> **Agent rule:** hand the one-time code to the user and wait. Never ask for a
> password, never ask for a token, never attempt a non-interactive auth flow
> with a token pasted into the terminal.

Verify with `gh auth status`.

### Step 3 — Make the workspace a repository

```bash
cd <workspace root>
git rev-parse --git-dir >/dev/null 2>&1 || git init -b main
```

Create a `.gitignore` if absent, so build output and phone data never get
committed:

```bash
cat > .gitignore <<'EOF'
# build output
ipa/build/
ipa/PocketBurp/pocketburp/
# runtime state created by the engine
db/
certs/
__pycache__/
*.pyc
.DS_Store
EOF
```

> Note: `ipa/PocketBurp/pocketburp/` is ignored because the build script
> overwrites it with real engine files. The committed `PLACEHOLDER.txt` inside it
> is not needed for the cloud build — `build-ipa.sh` recreates the folder. Do not
> "fix" this by un-ignoring it.

Also confirm the ignore did not eat the placeholder in a way that breaks the
Xcode project check:

```bash
python3 ipa/tools/check_pbxproj.py -v    # must print RESULT: OK
```

If that check fails because `PocketBurp/pocketburp/` is now empty, recreate the
placeholder once and force-add it:

```bash
mkdir -p ipa/PocketBurp/pocketburp
printf 'Engine is staged here at build time.\n' > ipa/PocketBurp/pocketburp/PLACEHOLDER.txt
git add -f ipa/PocketBurp/pocketburp/PLACEHOLDER.txt
python3 ipa/tools/check_pbxproj.py -v
```

### Step 4 — Create the repo and push

Ask the user one question first: **private or public?**
- **private** (default, recommended): 2,000 free Actions minutes/month; macOS
  runners bill at 10×, so a ~5-minute build costs ~50 minutes. Plenty.
- **public**: Actions is unlimited and free.

```bash
gh repo create pocketburp --private --source=. --remote=origin --push
# or: --public
```

Confirm the workflow landed on the default branch — `workflow_dispatch` only
shows up in the UI when the file exists on `main`:

```bash
git ls-files | grep -c '^.github/workflows/'
git log --oneline -1
```

### Step 5 — Run the build

```bash
gh workflow run build-ipa.yml
sleep 8
gh run list --workflow=build-ipa.yml --limit 1
```

Watch it to completion (this takes roughly 4–8 minutes on a cold runner):

```bash
RUN_ID=$(gh run list --workflow=build-ipa.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$RUN_ID" --exit-status || gh run view "$RUN_ID" --log-failed
```

If it fails: `gh run view "$RUN_ID" --log-failed` shows only the failing step.
Read the **first** `error:` line. Cross-check against the playbook in §7. Rerun
with `gh workflow run build-ipa.yml` after a fix. Do not rerun a failure without
changing something.

### Step 6 — Download and place the artifact

```bash
cd ~/Desktop
rm -rf pocketburp-artifact && mkdir pocketburp-artifact
gh run download "$RUN_ID" -n PocketBurp-ipa -D pocketburp-artifact
ls -lh pocketburp-artifact
mv pocketburp-artifact/PocketBurp.ipa ~/Desktop/PocketBurp.ipa
```

Then run every gate in §6 against `~/Desktop/PocketBurp.ipa`.

---

## 5. PATH B — NO CLI, BROWSER ONLY

Use this if `gh` cannot be installed. Exact clicks, no improvisation:

1. **Create the repo**: https://github.com/new → name `pocketburp` → Private →
   **do not** add README/.gitignore/license → Create.
2. **Upload the files**: on the empty repo page click **uploading an existing
   file**. In Finder, press `Cmd+Shift+.` to reveal hidden folders, then drag the
   **entire workspace folder's contents** (not the folder itself) into the page.
   Confirm `.github/workflows/build-ipa.yml` appears in the file list before
   committing — if it does not, the workflow will never run; use Path A instead.
3. Commit with message `initial import`.
4. **Actions** tab → left sidebar **Build PocketBurp.ipa** → **Run workflow** →
   branch `main` → **Run workflow**.
5. Wait ~5–8 min, refresh until the run is green.
6. Open the run → scroll to **Artifacts** → download **PocketBurp-ipa** (a zip) →
   unzip → `PocketBurp.ipa`.
7. Run every gate in §6 on that file.

If the artifact is missing, the run failed — open the failed step and read the
first `error:` line, then check §7.

---

## 6. SIGN-OFF GATES — run all of them on the downloaded file

Command Line Tools is enough for these; no Xcode needed.

```bash
IPA=~/Desktop/PocketBurp.ipa
set -e
ls -lh "$IPA"
rm -rf /tmp/v && mkdir -p /tmp/v && unzip -q "$IPA" -d /tmp/v
APP=/tmp/v/Payload/PocketBurp.app

for f in PocketBurp Info.plist \
         pocketburp/pocketburp.py \
         pocketburp/api.py \
         pocketburp/static/index.html \
         pocketburp/static/manifest.webmanifest \
         pocketburp/static/sw.js; do
  test -f "$APP/$f" && echo "  PASS  $f" || echo "  FAIL  missing $f"
done

echo "  engine files: $(find "$APP/pocketburp" -type f | wc -l | tr -d ' ')"
lipo -info "$APP/PocketBurp"
/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier"   "$APP/Info.plist"
/usr/libexec/PlistBuddy -c "Print :MinimumOSVersion"     "$APP/Info.plist"
/usr/libexec/PlistBuddy -c "Print :CFBundleDisplayName"  "$APP/Info.plist"
```

Every line must be `PASS`; engine files must be **≥ 20**; `lipo` must say
`arm64`; bundle id `com.shashank.pocketburp`; min iOS `15.0`; name `PocketBurp`.

**The gate that matters most**: `pocketburp/pocketburp.py` and
`pocketburp/static/index.html` inside the bundle. If either is missing the app
installs and launches to a blank recovery card. Report that as FAILED — do not
report success because the workflow went green; the workflow enforces the same
gates, but trust the downloaded file, not the status badge.

---

## 7. PLAYBOOK — CLOUD EDITION

| Symptom | Cause | Fix |
|---|---|---|
| `This job failed because of a scheduled macOS 14 runner image brownout` | runner retired 2026-11-02 | `runs-on: macos-15` |
| Workflow does not appear in Actions → Run workflow | file not on default branch, or dotfolder dropped in upload | confirm `git ls-files .github/workflows` lists it; push properly |
| `The workflow is not valid` | YAML indentation damaged during upload | restore from §Appendix A |
| `xcodebuild: error: ... does not contain an Xcode project` | step ran from the wrong directory | workflow already sets `working-directory: ipa`; check it was not edited |
| `Signing for "PocketBurp" requires a development team` | `CODE_SIGNING_ALLOWED=NO` was removed | restore it — unsigned is intended |
| `missing engine file: …` | staging step could not find `proxy.py` | `POCKETBURP_ENGINE_DIR` must point at the repo root (the workflow sets it) |
| `engine staging incomplete (n files)` | `static/` did not copy — partial upload to GitHub | verify `static/icons/*.png` and `static/sw.js` exist in the repo |
| `error: An error occurred during a build` with no detail | log truncated | download the **build-log** artifact from the same run |
| Runner queue stuck at "Waiting for a runner" | macOS runner capacity on the free tier | wait a few minutes; rerun |
| `gh: Not logged in` | auth was skipped | `gh auth login` (§4 Step 2) |
| Artifact download 404 | run was not successful, or artifact retention expired (30 days) | rerun the workflow |

---

## 8. AFTER YOU HAVE A VERIFIED .ipa — TELL THE USER THIS

The file is on the Desktop, unsigned. Install routes on a non-jailbroken iPhone:

- **LiveContainer** (no Apple ID, no expiry): install LiveContainer → tap **+**
  → **Import IPA** → pick `PocketBurp.ipa` → launch.
- **Sideloadly** (Mac or Windows, free Apple ID): drag the `.ipa` in, enter Apple
  ID, install. Then **Settings → General → VPN & Device Management → Trust**.
  Free accounts expire after **7 days** and cap at **3 apps**.
- **AltStore / AltServer**: same 7-day rule, auto-refresh while on the same WiFi.

And say this plainly, every time:

> The app is the window; iSH is the engine. PocketBurp's proxy has to `bind()`
> ports 8080 and 9090, and a WKWebView cannot do that. On the iPhone, install
> **iSH Shell** (App Store, free), run `cd ~/pocketburp && sh run.sh`, then open
> PocketBurp. If the app shows its recovery card, the engine is not running —
> tap **Open iSH Shell**, start it, come back and tap **Retry**. This is correct
> behaviour, not a bug in the build.

---

## 9. REPORT FORMAT — reply with exactly this

```
CLOUD BUILD : SUCCEEDED / FAILED
RUN         : <run id> | runner: macos-15 | duration <mm:ss>
WORKFLOW    : unmodified  |  changed (reason)
ARTIFACT    : <path>  (<size>)
ARCH        : <lipo output>
BUNDLE ID   : <id>   MIN IOS: <version>   NAME: <name>
SIGNING     : unsigned (ad-hoc) — sign at install time
ENGINE      : <n> files bundled; pocketburp.py yes/no; index.html yes/no
GATES       : <all PASS / which failed>
ENGINE SRC  : unmodified (git status clean)  |  modified: <files>
INSTALL     : <route, or "handed to user — awaiting install">
NEXT        : <one imperative line>
```

Add one `DEVIATION:` line per departure from this prompt, with the reason.

## 10. STOP AND ASK — do not guess

- `gh auth login` needs a browser and the user is absent.
- The user has not chosen private vs public.
- The same workflow step fails twice after applying the §7 fix.
- A fix would require editing the engine, the workflow (beyond the runner label),
  or adding any dependency.
- You are about to report success without having run every §6 gate.

---

## Appendix A — canonical workflow, for recovery only

Use **only** if `.github/workflows/build-ipa.yml` is missing or corrupted and
you cannot recover it from the workspace zip. It is the same file; do not
"improve" it while transcribing.

```yaml
name: Build PocketBurp.ipa

# Produces a real, installable PocketBurp.ipa in the cloud -- no local Xcode needed.
#   1. push this repo to GitHub
#   2. Actions -> "Build PocketBurp.ipa" -> Run workflow
#   3. download the "PocketBurp-ipa" artifact, unzip -> PocketBurp.ipa
#
# The artifact is UNSIGNED. LiveContainer / TrollStore install it directly;
# Sideloadly / AltStore / AltServer re-sign it with your own Apple ID.
#
# RUNNER: macos-15 (Xcode 16.x, iOS 18 SDK). Do NOT use macos-14 -- GitHub
# retired that image on 2026-11-02 and jobs targeting it no longer run.
# macos-latest currently resolves to macos-26; both are fine, macos-15 is pinned
# here because Xcode 16.4 is the most conservative toolchain for
# IPHONEOS_DEPLOYMENT_TARGET = 15.0.

on:
  workflow_dispatch:
  push:
    tags: ["v*"]

permissions:
  contents: read

jobs:
  build:
    runs-on: macos-15
    timeout-minutes: 30

    steps:
      - uses: actions/checkout@v4

      - name: Show toolchain
        run: |
          sw_vers
          xcodebuild -version
          xcrun --sdk iphoneos --show-sdk-version
          echo "xcode-select: $(xcode-select -p)"

      - name: Sanity-check the project before building
        run: python3 ipa/tools/check_pbxproj.py -v

      - name: Build .ipa
        working-directory: ipa
        run: |
          chmod +x build-ipa.sh
          POCKETBURP_ENGINE_DIR="$GITHUB_WORKSPACE" ./build-ipa.sh 2>&1 | tee build-full.log

      - name: Verify the bundle (hard gates)
        shell: bash
        run: |
          set -euo pipefail
          IPA=ipa/build/PocketBurp.ipa
          test -f "$IPA" || { echo "::error::no .ipa produced"; exit 1; }

          rm -rf /tmp/verify && mkdir -p /tmp/verify
          unzip -q "$IPA" -d /tmp/verify
          APP=/tmp/verify/Payload/PocketBurp.app
          test -d "$APP" || { echo "::error::Payload/PocketBurp.app missing"; exit 1; }

          # --- files that must exist ---------------------------------------
          for f in PocketBurp Info.plist \
                   pocketburp/pocketburp.py \
                   pocketburp/api.py \
                   pocketburp/static/index.html \
                   pocketburp/static/manifest.webmanifest \
                   pocketburp/static/sw.js; do
            test -f "$APP/$f" && echo "  PASS  $f" || { echo "::error::missing $f"; exit 1; }
          done

          COUNT=$(find "$APP/pocketburp" -type f | wc -l | tr -d ' ')
          echo "  engine files bundled: $COUNT"
          test "$COUNT" -ge 20 || { echo "::error::engine staging incomplete ($COUNT files)"; exit 1; }

          # --- binary + plist gates ----------------------------------------
          lipo -info "$APP/PocketBurp" | tee /tmp/lipo.txt
          grep -q arm64 /tmp/lipo.txt || { echo "::error::binary is not arm64"; exit 1; }

          BID=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier"  "$APP/Info.plist")
          MIN=$(/usr/libexec/PlistBuddy -c "Print :MinimumOSVersion"    "$APP/Info.plist")
          NAME=$(/usr/libexec/PlistBuddy -c "Print :CFBundleDisplayName" "$APP/Info.plist")
          echo "  bundle id : $BID"
          echo "  min iOS   : $MIN"
          echo "  name      : $NAME"
          test "$BID"  = "com.shashank.pocketburp" || { echo "::error::wrong bundle id"; exit 1; }
          test "$MIN"  = "15.0"                    || { echo "::error::wrong minimum iOS"; exit 1; }
          test "$NAME" = "PocketBurp"              || { echo "::error::wrong display name"; exit 1; }

          # --- signing state (unsigned is expected here) --------------------
          if codesign -dv "$APP" 2>&1 | grep -q "Authority="; then
            codesign -dv "$APP" 2>&1 | grep "Authority=" || true
          else
            echo "  PASS  unsigned (expected for CI; LiveContainer/Sideloadly will sign)"
          fi

          du -h "$IPA"
          echo "ALL GATES PASSED"

      - name: Keep the build log even on failure
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: build-log
          path: ipa/build-full.log
          if-no-files-found: warn

      - name: Upload the .ipa
        uses: actions/upload-artifact@v4
        with:
          name: PocketBurp-ipa
          path: ipa/build/PocketBurp.ipa
          if-no-files-found: error
          retention-days: 30
```
