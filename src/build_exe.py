#!/usr/bin/env python3
"""Build pipeline executables into scripts/ via PyInstaller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUT = ROOT / "scripts"
COMMON = [
    sys.executable,
    "-m",
    "PyInstaller",
    "--onefile",
    "--console",
    "--paths",
    str(SRC),
    "--distpath",
    str(OUT),
    "--workpath",
    str(ROOT / "build" / "pyinstaller"),
    "--specpath",
    str(ROOT / "build"),
]


def build(name: str, script: str) -> int:
    cmd = [
        *COMMON,
        "--clean",
        "--name",
        name,
        str(SRC / script),
    ]
    print("Running:", " ".join(cmd))
    return subprocess.call(cmd)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    rc = build("organize_by_playlists", "organize_by_playlists.py")
    if rc != 0:
        return rc
    return build("export_channel", "export_channel.py")


if __name__ == "__main__":
    raise SystemExit(main())
