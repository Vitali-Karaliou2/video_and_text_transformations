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
import re

from shared.project_paths import (
    channel_refs_match,
    channel_relative_ref,
    channel_root_containing,
    resolve_channel_ref,
)

COMMENT_PREFIXES = ("#", ";")
SETTINGS_CHANNEL_RE = re.compile(
    r"^(?P<head>\s*CHANNEL=)(?P<value>.*?)(?P<tail>\s*)$",
    re.IGNORECASE,
)


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


def repair_channel_in_settings(path: Path, expected_ref: str) -> bool:
    """Rewrite the first active CHANNEL= line when it names another folder."""
    if not path.is_file():
        return False
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig")
        encoding = "utf-8-sig"
    else:
        try:
            text = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = raw.decode("cp1251")
            encoding = "cp1251"
    changed = False
    fixed_first = False
    new_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if body.lstrip().startswith(COMMENT_PREFIXES):
            new_lines.append(line)
            continue
        match = SETTINGS_CHANNEL_RE.match(body)
        if match and not fixed_first:
            fixed_first = True
            current = match.group("value").strip()
            if not channel_refs_match(current, expected_ref):
                body = (
                    f"{match.group('head')}{expected_ref}{match.group('tail')}"
                )
                changed = True
            new_lines.append(body + ending)
        else:
            new_lines.append(line)
    if changed:
        path.write_bytes("".join(new_lines).encode(encoding))
    return changed


def reconcile_channel_ref(
    channels_root: Path,
    settings_path: Path,
    channel_from_settings: str,
    *,
    repair_settings: bool = True,
) -> str:
    """CHANNEL ref for a run script's settings file.

    The settings file sits in the channel's _run_scripts/, so the folder on
    disk is authoritative when CHANNEL still names a path left from a rename
    (or from synchronize_folders_in_bats after a temporary rename). Scripts
    that take @handle (refresh_summary) already resolve by channel_id; this
    brings the same reliability to CHANNEL-based scripts.
    """
    channels_root = channels_root.resolve()
    settings_path = settings_path.resolve()
    stored = (channel_from_settings or "").strip()
    on_disk = channel_root_containing(settings_path.parent, channels_root)
    disk_ref = (
        channel_relative_ref(on_disk, channels_root) if on_disk is not None
        else None
    )
    if stored and resolve_channel_ref(channels_root, stored) is not None:
        return stored
    if disk_ref:
        if stored and not channel_refs_match(stored, disk_ref):
            print(
                f"NOTE: CHANNEL in {settings_path} is still {stored!r}, but "
                f"this settings file sits under _channels\\{disk_ref}; using "
                "the folder on disk.",
                flush=True,
            )
            if repair_settings:
                repair_channel_in_settings(settings_path, disk_ref)
        return disk_ref
    return stored
