#!/usr/bin/env python3
"""Publish only this successful cloud build's main modules, with exact branch keys."""
import hashlib
import json
import os
import pathlib
import re

root = pathlib.Path("release")
tag = os.environ["DRIVER_TAG"]
repository = os.environ["GITHUB_REPOSITORY"]
commit = os.environ["GITHUB_SHA"]
modules = []
for name in (root / "built-kernels.txt").read_text().splitlines():
    match = re.fullmatch(r"(\d+\.\d+)-Android(\d+)", name)
    if not match:
        raise SystemExit(f"Invalid kernel key: {name}")
    path = root / "modules" / (name + ".ko")
    data = path.read_bytes()
    # ELF64 little-endian, ET_REL, AArch64, main lsdriver (not executor tests).
    if (data[:6] != b"\x7fELF\x02\x01" or data[16:20] != b"\x01\x00\xb7\x00"
            or b"name=lsdriver\x00" not in data):
        raise SystemExit(f"Not an AArch64 main module: {name}")
    modules.append({
        "kernel": match[1], "androidKernel": int(match[2]), "file": path.name,
        "url": f"https://github.com/{repository}/releases/download/{tag}/{path.name}",
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    })
if len(modules) != 7:
    raise SystemExit("Only complete seven-kernel builds can become the app's latest release")
manifest = {"schema": 1, "driver": "lsdriver", "touchProtocol": "LST1",
            "tag": tag, "commit": commit, "modules": modules}
(root / "driver-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
(root / "release-notes.md").write_text(
    "Main lsdriver modules built entirely on GitHub Actions.\n\n"
    "Match the Android **kernel branch** and Linux major.minor from `uname -r`; "
    "ignore patch/vendor suffixes. Do not match the phone OS version.\n\n"
    "Includes LST1 physical touch snapshots for the overlay. "
    "driver-manifest.json supplies per-module SHA-256 and download URLs for the app.\n\n"
    f"Source: {commit}\n"
)
