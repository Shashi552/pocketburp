# Building & installing PocketBurp.app

Everything here runs **on your own Mac**. No remote shell, no tunnel, no public
link — and you should not create one. See "Why not a remote shell" at the bottom.

---

## 0. One-time Mac setup

> **Check your macOS version first: `sw_vers -productVersion`.**
>
> - **13 Ventura or newer** — everything below works.
> - **12 Monterey or older** — stop. Xcode 15+ cannot install on macOS 12; the
>   newest Xcode that runs there is **14.2**, long gone from the App Store.
>   Command Line Tools alone cannot build an iOS app. **Use Route 4 (cloud build)
>   instead** — it needs no local Xcode at all. `uname -m` + `sw_vers` decided
>   this; it is a version ceiling, not a missing install.

```bash
xcode-select --install            # only if you skipped Xcode
open -a Xcode                     # let it finish "Installing components…" once
sudo xcodebuild -runFirstLaunch   # accept the licence (prompts for password)
xcodebuild -version               # should print "Xcode 15.x" or newer
```

Then confirm the project is sane **before** spending build time:

```bash
cd ipa
./build-on-mac.sh --doctor
```

---

## 1. Build the `.ipa`

```bash
cd ipa

# unsigned — the route for LiveContainer / TrollStore
./build-on-mac.sh

# signed with your Apple ID certificate (Team ID from your setup)
./build-on-mac.sh --team MPZ586FDM5
```

Output: `ipa/build/PocketBurp.ipa`

The script stages the Python engine, archives Release/arm64, packages
`Payload/PocketBurp.app`, then prints the arch, bundle id, signature and how many
engine files made it inside. If anything fails it shows the `error:` lines and
points at `build/build.log`.

### If it fails, this is the fix loop
Run the report script and paste the **whole** text back to me:

```bash
cd ipa && ./verify-build.sh > ~/Desktop/pocketburp-report.txt 2>&1
open ~/Desktop/pocketburp-report.txt
```

It prints Xcode version, SDK, project structure check, staged file count,
`lipo -info`, `codesign -dv`, Info.plist keys and the build log tail — nothing
secret (no Apple ID password, no profile contents, no device tokens).
That replaces a shell session completely.

---

## 2. Install — three routes

### Route 1 — LiveContainer (fits your existing setup)
1. Build **unsigned**: `./build-on-mac.sh`
2. Get `PocketBurp.ipa` to the iPhone: AirDrop, iCloud Drive, or
   `xcrun devicectl list devices` → Files app.
3. LiveContainer → **+** → *Import IPA* → `PocketBurp.ipa`.
4. Launch. LiveContainer signs it with your Apple ID certificate (the same one
   already used for iSH/Delta/UTM-SE), so no 7-day re-sign dance.

### Route 2 — Sideloadly / AltStore (free Apple ID)
* Drag the `.ipa` in, enter your Apple ID, install.
* **Free-account limits, so it's not a surprise later:** 7-day signing expiry,
  max 3 sideloaded apps, 10 App IDs per week.
* After installing: **Settings → General → VPN & Device Management → trust your
  developer certificate**, otherwise the app won't open.

### Route 3 — Xcode direct to a connected iPhone
```bash
cd ipa
./build-on-mac.sh --team MPZ586FDM5 --install
# or open PocketBurp.xcodeproj, pick your iPhone, press ⌘R
```
`--install` uses `xcrun devicectl device install app` (Xcode 15+). If it's
missing, use **Window → Devices and Simulators** and drag the `.ipa` onto the device.

### Route 4 — no Mac needed (or macOS too old for Xcode)
Push the repo to GitHub → **Actions → Build PocketBurp.ipa → Run workflow** →
download the `PocketBurp-ipa` artifact. Unsigned, LiveContainer-ready.

The workflow runs on **`macos-15`**. Never set `runs-on: macos-14`: GitHub
retired that runner image on **2 November 2026**, and jobs that target it fail
immediately with *"scheduled macOS 14 runner image brownout"*. The workflow also
runs `check_pbxproj.py` first, enforces the artifact gates (engine entry + GUI +
manifest + service worker + ≥20 files + arm64 + bundle id + min iOS), and uploads
the full build log as a separate artifact so a failure is diagnosable.

Agent-driven, end to end: **`ipa/ANTIGRAVITY-CI-PROMPT.md`**.

---

## 2b. Moving the engine into iSH Shell (sandbox crossing)

The `.ipa` bundles the Python source code inside `PocketBurp.app`'s sandbox. However, **iSH is a separate iOS app with its own sandboxed filesystem and cannot read another app's bundle.** Therefore, `cd ~/pocketburp` will not exist inside iSH until you transfer the engine files into iSH's sandbox.

We package a clean standalone engine zip: **`pocketburp-engine-for-iSH.zip`** (contains the frozen engine and static assets; no keys, no databases, no caches).

### Step-by-step transfer sequence:

1. **Move `pocketburp-engine-for-iSH.zip` to the iPhone**:
   - AirDrop `pocketburp-engine-for-iSH.zip` to your iPhone, or save it to iCloud Drive.
   - In the iOS **Files** app, tap `pocketburp-engine-for-iSH.zip` to extract the `pocketburp/` folder.

2. **Mount the folder in iSH**:
   Open **iSH Shell** and run:
   ```sh
   apk update && apk add python3 openssl
   mount -t ios . /mnt
   ```
   A native iOS document picker sheet opens. Select the extracted `pocketburp` folder.

3. **Copy the engine into iSH home directory**:
   ```sh
   cp -R /mnt/pocketburp ~/pocketburp
   cd ~/pocketburp
   sh run.sh
   ```

4. **Launch PocketBurp**:
   - Leave iSH running in the background.
   - Open PocketBurp. If you see the recovery card ("Engine not running"), tap **Retry** once the iSH engine reports listening on ports 8080 and 9090.

---

## 3. What the app does on first launch

| Situation | Behaviour |
|---|---|
| Engine already running in iSH | Splash → WebView at `127.0.0.1:9090` → full GUI |
| Engine not running | Native card: **Engine not running**, Retry, Open iSH Shell (copies the exact command) |
| iSH app absent | Retry keeps probing; the alert tells you what to run |

The app is the **window**, not the proxy. iOS only lets a real process bind ports
8080/9090, so the MITM engine lives in iSH. `EngineBridge.launchPythonEngine()`
in `PocketBurpApp.swift` is the single seam if you later embed the iSH xcframework
and run the engine in-process.

---

## 4. Why not a remote shell

You offered a public shell link. Three reasons I won't take it, and one that
matters more than the rest:

1. **I can't reach it.** My tools run inside a sandbox with no arbitrary outbound
   shell/SSH/WebSocket client. A public URL gives me nothing to type into.
2. **It wouldn't help.** The build is deterministic: either `xcodebuild` prints
   `ARCHIVE SUCCEEDED`, or it prints an `error:` line. `verify-build.sh` already
   captures that — the round trip is one paste, not a session.
3. **You shouldn't expose it.** A reverse shell on your Mac, reachable from the
   internet, is a full-takeover risk — far more dangerous than the bugs you're
   researching. Tunnel providers get scanned within minutes.

If you ever *do* need help from a human contractor, use screen sharing with a
named individual over your own network, not a public link posted in a chat.

---

## 5. Troubleshooting cheat-sheet

| Symptom | Cause / fix |
|---|---|
| `Signing for "PocketBurp" requires a development team` | add `--team MPZ586FDM5`, or keep it unsigned for LiveContainer |
| `No such module` / Swift errors | you likely have only Command Line Tools; install full Xcode |
| `The request was denied by service delegate (SBMainWorkspace)` | app launched but device lacks the entitlement — re-sign with your own cert |
| Installs, launches, shows recovery card | iSH isn't running `sh run.sh`; open iSH, tap Retry |
| GUI loads but no traffic in History | Wi-Fi proxy not set to `127.0.0.1:8080`, or Scope is filtering |
| `Unable to install … not supported` | free-account 3-app limit, or the IPA is signed for a different Team ID |
| App dies 7 days later | free provisioning expiry — re-sign/relaunch through LiveContainer |
