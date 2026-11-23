#!/usr/bin/env python3
"""Build the extension package for Mozilla (addons.mozilla.org) and sync the shared files.

    python tools/pack.py          -> dist/anime-remote-<version>.zip  (upload this to AMO to get it signed)

extension/protocol.js is the source; docs/protocol.js (the phone page) must be an exact copy.
"""
import json
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")
DOCS = os.path.join(ROOT, "docs")
DIST = os.path.join(ROOT, "dist")


def main():
    src = os.path.join(EXT, "protocol.js")
    dst = os.path.join(DOCS, "protocol.js")
    shutil.copyfile(src, dst)
    shutil.copyfile(os.path.join(EXT, "icons", "icon.svg"), os.path.join(DOCS, "icon.svg"))

    with open(os.path.join(EXT, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    version = manifest["version"]
    missing = []
    for part in manifest["background"]["scripts"] + [js for cs in manifest["content_scripts"] for js in cs["js"]] + \
            [manifest["browser_action"]["default_popup"], manifest["browser_action"]["default_icon"]] + list(manifest["icons"].values()):
        if not os.path.isfile(os.path.join(EXT, part)):
            missing.append(part)
    if missing:
        sys.exit("missing files in extension/: " + ", ".join(missing))

    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "anime-remote-%s.zip" % version)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(EXT):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(base, name)
                rel = os.path.relpath(full, EXT).replace(os.sep, "/")
                z.write(full, rel)
    print("protocol.js synced to docs/")
    print("package: %s (%d bytes)" % (out, os.path.getsize(out)))


if __name__ == "__main__":
    main()
