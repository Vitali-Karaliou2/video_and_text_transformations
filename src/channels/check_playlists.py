#!/usr/bin/env python3
"""Validate per-channel _cache/playlists.json and _playlists/ folder layout."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from channels.channel_playlists import (  # noqa: E402
    ensure_playlist_folders,
    is_misc_folder,
    iter_channel_roots,
    known_playlist_folders,
    load_playlists_cache,
    missing_playlist_folders,
    playlists_cache_path,
    sync_channel_playlists,
    validate_playlist_folders,
)
from shared.yt_dlp_opts import YOUTUBE_SYSTEM_CERTS  # noqa: E402
from shared.project_paths import (  # noqa: E402
    WORKSPACE_ROOT,
    channel_playlists_dir,
    channel_relative_ref,
    channels_dir,
    describe_channels_layout,
    resolve_channel_ref,
)

CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")

KNOWN_CHANNELS = {
    "_AleksanderLamkov": ("UCmjFlNOWMGP5VB0Q2RARaVA", "@AleksanderLamkov"),
    "_Ekaterina_Schulmann": ("UCL1rJ0ROIw9V1qFeIN0ZTZQ", "@Ekaterina_Schulmann"),
    "_VladilenMinin": ("UCg8ss4xW9jASrqWGP30jXiw", "@VladilenMinin"),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check _cache/playlists.json and _playlists/ folders for each channel."
        )
    )
    parser.add_argument(
        "channel",
        nargs="?",
        help="Optional channel ref under _channels/ "
        "(e.g. _VladilenMinin or AI_for_Game_Design\\_BuildingAeon)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh _cache/playlists.json from YouTube when it differs",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create missing empty playlist folders under _playlists/",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=WORKSPACE_ROOT,
        help="Workspace root (default: parent of src/)",
    )
    parser.add_argument(
        "--yt-dlp",
        default="yt-dlp",
        help="yt-dlp executable (default: yt-dlp)",
    )
    parser.add_argument(
        "--layout",
        action="store_true",
        help="Print the _channels/ tree (containers vs channel folders) and exit",
    )
    return parser.parse_args(argv)


def run_yt_dlp(args: argparse.Namespace, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [args.yt_dlp, *YOUTUBE_SYSTEM_CERTS, *extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def channel_roots(args: argparse.Namespace) -> list[Path]:
    root = channels_dir(args.workspace)
    if args.channel:
        resolved = resolve_channel_ref(root, args.channel)
        return [resolved] if resolved else [root / args.channel]
    return iter_channel_roots(root)


def list_misc_dirs(channel_root: Path, known: set[str]) -> list[str]:
    playlists_root = channel_playlists_dir(channel_root)
    if not playlists_root.is_dir():
        return []
    misc_dirs: list[str] = []
    for child in sorted(playlists_root.iterdir()):
        if child.is_dir() and is_misc_folder(child.name, known):
            misc_dirs.append(child.name)
    return misc_dirs


def check_channel(channel_root: Path, args: argparse.Namespace) -> int:
    issues = 0
    cache_path = playlists_cache_path(channel_root)
    rel_cache = cache_path.relative_to(args.workspace)

    print(f"\n[{channel_relative_ref(channel_root, channels_dir(args.workspace))}]")
    print(f"  Cache file: {rel_cache}")

    cached = load_playlists_cache(cache_path) if cache_path.is_file() else None
    if cached is None:
        print("  Status:   MISSING playlists.json")
        print("  Hint:     run _run_scripts\\update_playlists.bat to fetch from YouTube")
        issues += 1
        if not args.update:
            return issues

    channel_id = cached.get("channel_id") if cached else None
    channel_handle = cached.get("channel_handle") if cached else None
    channel_name = cached.get("channel_name") if cached else None
    if not channel_id and channel_root.name in KNOWN_CHANNELS:
        channel_id, channel_handle = KNOWN_CHANNELS[channel_root.name]

    if args.update:
        if not channel_id or not CHANNEL_ID_RE.fullmatch(channel_id):
            print("  Status:   SKIP update (channel_id unknown)")
            return issues
        print(f"  Action:   updating from YouTube ({channel_id})")
        cached = sync_channel_playlists(
            channel_root,
            channel_id=channel_id,
            channel_handle=channel_handle,
            channel_name=channel_name,
            run_yt_dlp=lambda *a, **k: run_yt_dlp(args, *a, **k),
            force_fetch=True,
            update_cache=True,
            create_folders=args.create,
        )
    elif cached is None:
        return issues

    if channel_handle:
        print(f"  Handle:   {channel_handle}")
    if channel_id:
        print(f"  Channel:  {channel_id}")
    if cached.get("last_checked_date"):
        print(f"  Checked:  {cached['last_checked_date']}")

    known = known_playlist_folders(cached)
    playlist_count = len(cached.get("playlists", []))
    misc_dirs = list_misc_dirs(channel_root, known)
    missing = missing_playlist_folders(channel_root, cached)

    print(f"  Playlists in cache: {playlist_count}")
    print(f"  Folders in _playlists/: {playlist_count - len(missing)} present, {len(missing)} missing")
    if misc_dirs:
        print(f"  Misc folders:       {', '.join(misc_dirs)}")
    if missing:
        print(f"  Missing folders:    {', '.join(missing)}")
        # Empty playlist folders are deferred by default (created when a
        # video of that playlist is first transcribed or downloaded), so
        # a missing folder is not an error unless the user asked to create
        # them with --create.
        if args.create:
            issues += len(missing)
        else:
            print(
                "  Note:     missing folders are normal until a video of "
                "that playlist is transcribed/downloaded "
                "(--create makes them all now)"
            )

    folder_errors = validate_playlist_folders(channel_root, cached)
    for message in folder_errors:
        print(f"  ERROR: {message.replace('Folder _playlists/', 'Unknown folder: ')}")
        issues += 1

    if args.create and missing:
        created = ensure_playlist_folders(
            channel_root, cached.get("playlists", []), create=True
        )
        if created:
            print(f"  Created folders:    {', '.join(created)}")
            issues = max(0, issues - len(created))

    if issues == 0:
        print(f"  Result:   OK")
    else:
        print(f"  Result:   {issues} issue(s)")
    return max(issues, 0)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    channels_root = channels_dir(args.workspace)
    if args.layout:
        for line in describe_channels_layout(channels_root):
            print(line)
        return 0

    roots = channel_roots(args)

    print("Playlist cache check")
    print(f"Workspace:  {args.workspace.resolve()}")
    print(f"Channels:   {channels_root.resolve()}")
    if args.channel:
        print(f"Filter:     {args.channel}")
    else:
        print(f"Filter:     all channel folders ({len(roots)})")
    print(
        "Rules:      each _playlists/* folder must appear in _cache/playlists.json "
        "(except misc/_misc/...)"
    )

    if not roots:
        print("\nNo channel folders found under _channels/.", file=sys.stderr)
        return 1

    total_issues = 0
    checked = 0
    ok = 0
    for channel_root in roots:
        if not channel_root.is_dir():
            print(f"\n[{channel_relative_ref(channel_root, channels_dir(args.workspace))}]")
            print("  Result:   folder not found")
            total_issues += 1
            continue
        checked += 1
        channel_issues = check_channel(channel_root, args)
        total_issues += channel_issues
        if channel_issues == 0:
            ok += 1

    print("\n--- Summary ---")
    print(f"Channels checked: {checked}")
    print(f"OK:               {ok}")
    print(f"With issues:      {checked - ok}")
    if total_issues == 0:
        print("Overall result:   PASSED")
    else:
        print(f"Overall result:   FAILED ({total_issues} issue(s))")
        print("Hint: run _run_scripts\\update_playlists.bat to refresh cache and create folders")

    return 1 if total_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
