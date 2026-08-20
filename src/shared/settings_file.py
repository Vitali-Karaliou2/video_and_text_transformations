#!/usr/bin/env python3
"""The hand-edited settings of a run script, kept in a text file of its own.

A bat file cannot carry a Russian channel description: cmd.exe reads the bat
in the console code page and the value reaches Python as garbage, and the one
obvious cure - chcp 65001 - switches the console to a raster font and breaks
Cyrillic on screen. So the values a run script asks the user to edit live in
a plain UTF-8 file next to it, one KEY=value per line, and are read from here
rather than passed through a cmd variable.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

COMMENT_PREFIXES = ("#", ";")


def read_settings(path: Path, allowed: Collection[str]) -> dict[str, str]:
    """KEY=value pairs from a settings file; of a repeated key the first wins.

    Blank lines and lines opening with '#' or ';' are comments - that is where
    an earlier value is parked instead of being deleted, and being first is
    what makes a value the current one. A key outside `allowed` is a typo and
    stops the run: silently ignoring it would put the channel folder in the
    wrong place.
    """
    if not path.is_file():
        raise SystemExit(f"Settings file not found: {path}")
    settings: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith(COMMENT_PREFIXES):
            continue
        if "=" not in text:
            raise SystemExit(
                f"{path}, line {number}: expected KEY=value or a comment "
                f"opening with '#', got:\n{line}"
            )
        key, value = text.split("=", 1)
        key = key.strip().upper()
        if key not in allowed:
            raise SystemExit(
                f"{path}, line {number}: unknown setting {key!r}; this file "
                f"takes {', '.join(sorted(allowed))}"
            )
        settings.setdefault(key, value.strip())
    return settings


def settings_beside(bat_path: Path) -> Path:
    """The settings file that belongs next to a bat: <stem>.settings.txt."""
    return bat_path.with_name(bat_path.stem + ".settings.txt")


def write_settings(path: Path, lines: str) -> None:
    """Write a settings file as UTF-8 without a BOM, with Windows newlines."""
    path.write_bytes(lines.replace("\n", "\r\n").encode("utf-8"))
