#!/usr/bin/env python3
"""Check, and if needed repair, the folder paths hardcoded in the bat files.

Three things in a bat go stale when folders move:

- `cd /d <project root>` - the absolute path of the project, which changes
  when the whole project is moved to another folder or drive;
- `set "CHANNEL=<container>\\<channel>"` - the channel folder relative to
  _channels/, which changes when a channel is regrouped into another
  container (or into no container at all);
- `set "PLAYLIST=<folder>"` - the playlist folder under the channel's
  _playlists/, which changes when a playlist folder is renamed.

The script walks every *.bat in the project, compares those values with the
folders that exist now and rewrites the stale ones in place; the encoding of
each file and its line endings are preserved. Absolute paths that point
somewhere else inside the project (not the root) are rebased too, as long as
the target can be identified without guessing - otherwise they are only
reported, together with anything else that needs a human decision.

Usage:
  python src/synchronize_folders_in_bats.py           # check and repair
  python src/synchronize_folders_in_bats.py --check   # report only

Exit code 1 means something is still out of sync: in --check mode anything
that would be rewritten, otherwise only what could not be repaired.

Automation: _run_scripts/synchronize_folders_in_bats.bat
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PureWindowsPath

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from project_paths import (
    WORKSPACE_ROOT,
    channel_playlists_dir,
    channel_relative_ref,
    channels_dir,
    is_channel_root,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SKIP_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", "node_modules"})
# The path is the rest of the line: an unquoted cd target may well contain
# spaces, as cmd takes everything up to the line end.
CD_RE = re.compile(
    r'^(?P<head>\s*cd\s+(?:/d\s+)?)(?P<path>"[^"]*"|.+?)(?P<tail>\s*)$',
    re.IGNORECASE,
)
CHANNEL_RE = re.compile(
    r'^(?P<head>\s*set\s+"CHANNEL=)(?P<value>[^"]*)(?P<tail>".*)$',
    re.IGNORECASE,
)
PLAYLIST_RE = re.compile(
    r'^(?P<head>\s*set\s+"PLAYLIST=)(?P<value>[^"]*)(?P<tail>".*)$',
    re.IGNORECASE,
)


@dataclass
class FileResult:
    changed: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def normalized(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def same_ref(left: str, right: str) -> str:
    """Compare two channel/playlist refs the way Windows would."""
    def clean(value: str) -> str:
        return normalized(value.strip().strip("\\/").replace("/", "\\"))

    return clean(left) == clean(right)


def inside_workspace(path: str | Path) -> bool:
    root = normalized(WORKSPACE_ROOT)
    text = normalized(path)
    return text == root or text.startswith(root + os.sep)


def iter_bat_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.casefold().endswith(".bat"):
                found.append(Path(current) / name)
    return found


@lru_cache(maxsize=1)
def directory_index() -> dict[str, list[Path]]:
    """Every folder in the project, grouped by its (lower-case) name."""
    index: dict[str, list[Path]] = {}
    for current, dirnames, _ in os.walk(WORKSPACE_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in dirnames:
            index.setdefault(name.casefold(), []).append(Path(current) / name)
    return index


def rebase(old: str) -> tuple[Path | None, str]:
    """Where an absolute path written in a bat points to in the current tree.

    The longest tail of the old path that exists under the project root wins;
    when nothing matches, a folder with the same name is accepted only if the
    project has exactly one.
    """
    parts = PureWindowsPath(old).parts
    for start in range(1, len(parts)):
        candidate = WORKSPACE_ROOT.joinpath(*parts[start:])
        if candidate.exists():
            return candidate, "found under the project root"
    if not parts:
        return None, "not a usable path"
    matches = directory_index().get(parts[-1].casefold(), [])
    if len(matches) == 1:
        return matches[0], "the only folder with that name in the project"
    if len(matches) > 1:
        return None, f"{len(matches)} folders share that name"
    return None, "no such folder in the project"


def owning_channel_root(path: Path) -> Path | None:
    """The channel folder a bat belongs to, if it lies inside one."""
    root = channels_dir(WORKSPACE_ROOT)
    if not inside_workspace(path):
        return None
    current = path.resolve().parent
    while inside_workspace(current) and normalized(current) != normalized(root):
        if is_channel_root(current):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def decode_bat(raw: bytes) -> tuple[str, str]:
    """Text of a bat plus the codec to write it back with.

    cp1251 is only a fallback that maps every byte, so even a wrong guess
    round-trips the lines the script does not touch.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1251"), "cp1251"


def split_ending(line: str) -> tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def fix_cd(body: str, result: FileResult) -> str:
    match = CD_RE.match(body)
    if not match:
        return body
    quoted = match.group("path")
    value = quoted.strip('"')
    # %~dp0-style and relative targets already survive a move.
    if "%" in value or not PureWindowsPath(value).is_absolute():
        return body
    if inside_workspace(value) and Path(value).exists():
        return body

    target, why = rebase(value)
    if target is None:
        # Every "cd /d" in this project takes the bat to the project root.
        if "/d" in match.group("head").casefold():
            target, why = WORKSPACE_ROOT, "assumed to be the project root"
        else:
            result.warnings.append(
                f'cd target "{value}" was not found in the project ({why}); '
                "fix this line by hand"
            )
            return body
    if normalized(target) == normalized(value):
        return body

    text = str(target)
    formatted = f'"{text}"' if " " in text else text
    result.notes.append(f'cd: "{value}" -> "{text}" ({why})')
    return f"{match.group('head')}{formatted}{match.group('tail')}"


def fix_channel(body: str, channel_root: Path | None, result: FileResult) -> str:
    if channel_root is None:
        return body
    match = CHANNEL_RE.match(body)
    if not match:
        return body
    current = match.group("value")
    expected = channel_relative_ref(channel_root, channels_dir(WORKSPACE_ROOT))
    if same_ref(current, expected):
        return body
    result.notes.append(f'CHANNEL: "{current}" -> "{expected}"')
    return f"{match.group('head')}{expected}{match.group('tail')}"


def fix_playlist(body: str, channel_root: Path | None, result: FileResult) -> str:
    if channel_root is None:
        return body
    match = PLAYLIST_RE.match(body)
    if not match:
        return body
    current = match.group("value").strip()
    playlists = channel_playlists_dir(channel_root)
    if not current or (playlists / current).is_dir():
        return body

    leaf = current.replace("/", "\\").rstrip("\\").split("\\")[-1]
    if leaf != current and (playlists / leaf).is_dir():
        result.notes.append(f'PLAYLIST: "{current}" -> "{leaf}"')
        return f"{match.group('head')}{leaf}{match.group('tail')}"
    result.warnings.append(
        f'PLAYLIST "{current}" is not a folder under '
        f"_playlists/ of this channel; fix this line by hand"
    )
    return body


def sync_bat(path: Path, *, apply: bool) -> FileResult:
    result = FileResult()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        result.warnings.append(f"could not be read: {exc}")
        return result
    text, encoding = decode_bat(raw)
    channel_root = owning_channel_root(path)

    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        body, ending = split_ending(line)
        body = fix_cd(body, result)
        body = fix_channel(body, channel_root, result)
        body = fix_playlist(body, channel_root, result)
        lines.append(body + ending)

    new_text = "".join(lines)
    result.changed = new_text != text
    if result.changed and apply:
        try:
            path.write_bytes(new_text.encode(encoding))
        except OSError as exc:
            result.changed = False
            result.warnings.append(f"could not be written: {exc}")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check and repair the folder paths hardcoded in the project bats."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report what is out of sync, do not touch any file",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    bats = iter_bat_files(WORKSPACE_ROOT)
    print(f"Project root: {WORKSPACE_ROOT}", flush=True)
    mode = " (check only, nothing is written)" if args.check else ""
    print(f"Bat files found: {len(bats)}{mode}", flush=True)
    print("", flush=True)

    stale = 0
    manual = 0
    for path in bats:
        result = sync_bat(path, apply=not args.check)
        if not result.notes and not result.warnings:
            continue
        print(str(path.relative_to(WORKSPACE_ROOT)), flush=True)
        for note in result.notes:
            print(f"  {note}", flush=True)
        for warning in result.warnings:
            print(f"  WARNING: {warning}", flush=True)
        print("", flush=True)
        if result.notes:
            stale += 1
        manual += len(result.warnings)

    if not stale and not manual:
        print("Everything is in sync; nothing to change.", flush=True)
        return 0
    verb = "would be updated" if args.check else "updated"
    print(f"Files {verb}: {stale}", flush=True)
    if manual:
        print(f"Lines needing a manual fix: {manual}", flush=True)
    if args.check and stale:
        print(
            "Run the same command without --check to apply these changes.",
            flush=True,
        )
    return 1 if (manual or (args.check and stale)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
