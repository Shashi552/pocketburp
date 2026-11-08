#!/usr/bin/env python3
"""
PocketBurp -- ipa/tools/check_pbxproj.py

Static sanity check for the Xcode project, runnable on Linux/Windows/macOS
without Xcode. It does NOT replace `xcodebuild`, it just catches the typo class
of failure (dangling UUIDs, unbalanced braces, duplicate objects, a target with
no sources) before you spend time on a Mac build.

    python3 ipa/tools/check_pbxproj.py [-v]
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PBX = os.path.join(ROOT, "PocketBurp.xcodeproj", "project.pbxproj")

UUID_RE = re.compile(r"\b([0-9A-F]{24})\b")


def strip_comments(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def defined_objects(text):
    """Every `<UUID> = { ... };` at object level defines an object."""
    return set(re.findall(r"^\t\t([0-9A-F]{24})\s*=\s*\{", text, flags=re.M))


def main():
    verbose = "-v" in sys.argv
    if not os.path.isfile(PBX):
        print("FAIL: %s not found" % PBX)
        return 1

    raw = open(PBX, encoding="utf-8").read()
    text = strip_comments(raw)
    errors, warnings = [], []

    # --- brace / paren balance -------------------------------------------
    for open_c, close_c, name in (("{", "}", "braces"), ("(", ")", "parens")):
        depth = 0
        for ch in text:
            if ch == open_c:
                depth += 1
            elif ch == close_c:
                depth -= 1
                if depth < 0:
                    errors.append("extra closing %s" % close_c)
                    break
        if depth != 0:
            errors.append("unbalanced %s (depth %d at EOF)" % (name, depth))

    # --- object graph ----------------------------------------------------
    objects = defined_objects(text)
    if not objects:
        errors.append("no objects parsed -- file layout changed?")

    referenced = set()
    for line in text.splitlines():
        stripped = line.strip()
        # skip the definition line itself
        if re.match(r"^[0-9A-F]{24}\s*=", stripped):
            continue
        referenced.update(UUID_RE.findall(stripped))

    dangling = sorted(referenced - objects)
    for uid in dangling:
        errors.append("dangling UUID %s referenced but never defined" % uid)

    unused = sorted(objects - referenced)
    # rootObject is referenced, so anything unused is genuinely orphaned
    for uid in unused:
        warnings.append("object %s defined but never referenced" % uid)

    # --- required pieces -------------------------------------------------
    # These labels live inside /* comments */, so check the raw file for them
    cheap_checks = {
        "PBXProject section": "isa = PBXProject" in text,
        "PBXNativeTarget": "isa = PBXNativeTarget" in text,
        "PBXSourcesBuildPhase": "isa = PBXSourcesBuildPhase" in text,
        "PBXResourcesBuildPhase": "isa = PBXResourcesBuildPhase" in text,
        "rootObject": bool(re.search(r"rootObject = [0-9A-F]{24}", text)),
        "Swift source in Sources phase": "PocketBurpApp.swift in Sources" in raw,
        "AppIcon asset catalog name": "ASSETCATALOG_COMPILER_APPICON_NAME = AppIcon" in text,
        "Info.plist path": "INFOPLIST_FILE = PocketBurp/Info.plist" in text,
        "bundle id": "PRODUCT_BUNDLE_IDENTIFIER = com.shashank.pocketburp" in text,
        "engine folder in Resources": "pocketburp in Resources" in raw,
    }
    for label, ok in cheap_checks.items():
        if not ok:
            errors.append("missing: %s" % label)

    # --- files referenced on disk ---------------------------------------
    disk_expect = [
        "PocketBurp/PocketBurpApp.swift",
        "PocketBurp/Info.plist",
        "PocketBurp/Assets.xcassets/AppIcon.appiconset/Contents.json",
        "PocketBurp/Assets.xcassets/AppIcon.appiconset/icon-1024.png",
        "PocketBurp.xcodeproj/xcshareddata/xcschemes/PocketBurp.xcscheme",
    ]
    for rel in disk_expect:
        if not os.path.isfile(os.path.join(ROOT, rel)):
            errors.append("missing file on disk: %s" % rel)

    # Info.plist must be well-formed XML
    try:
        import plistlib
        with open(os.path.join(ROOT, "PocketBurp", "Info.plist"), "rb") as fh:
            plist = plistlib.load(fh)
        for key in ("CFBundleIdentifier", "CFBundleExecutable", "UISupportedInterfaceOrientations"):
            if key not in plist:
                errors.append("Info.plist missing key: %s" % key)
    except Exception as exc:                      # noqa: BLE001
        errors.append("Info.plist is not parseable: %s" % exc)

    # --- icon sanity -----------------------------------------------------
    try:
        from PIL import Image
        icon = os.path.join(ROOT, "PocketBurp/Assets.xcassets/AppIcon.appiconset/icon-1024.png")
        with Image.open(icon) as img:
            if "A" in img.getbands() or img.mode != "RGB":
                warnings.append("AppIcon has an alpha channel (%s) -- App Store "
                                "rejects it; sideloading is fine" % img.mode)
            if img.size != (1024, 1024):
                errors.append("icon-1024.png is %s, expected 1024x1024" % (img.size,))
    except ImportError:
        warnings.append("Pillow not installed -- skipped icon checks")
    except Exception as exc:                      # noqa: BLE001
        warnings.append("icon check skipped: %s" % exc)

    # --- report ----------------------------------------------------------
    print("PocketBurp Xcode project check")
    print("  file      : %s" % PBX)
    print("  objects   : %d" % len(objects))
    print("  references: %d" % len(referenced))
    print()
    for w in warnings:
        print("  warn  %s" % w)
    for e in errors:
        print("  ERROR %s" % e)
    if verbose and not errors:
        print("\n  all checks passed")
    print()
    if errors:
        print("RESULT: FAIL (%d error%s)" % (len(errors), "" if len(errors) == 1 else "s"))
        return 1
    print("RESULT: OK -- project structure is coherent, safe to open in Xcode")
    return 0


if __name__ == "__main__":
    sys.exit(main())
