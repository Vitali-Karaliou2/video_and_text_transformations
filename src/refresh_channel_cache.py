#!/usr/bin/env python3
"""Refresh per-channel video cache (_cache/videos.json).

Default: smart incremental refresh (prepend new videos when possible).
Use --force for a full re-fetch from YouTube.

Examples:
  python src/refresh_channel_cache.py @Ekaterina_Schulmann
  python src/refresh_channel_cache.py @Ekaterina_Schulmann --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from channel_playlists import load_video_playlist_map, migrate_legacy_browse_cache
from channel_browse import fetch_channel_name
from get_summary_for_channel import (
    resolve_channel_handle,
    resolve_channel_id,
    resolve_output_folder,
    resolve_playlist_map,
)
from project_paths import WORKSPACE_ROOT, channels_dir
from video_cache import ensure_video_cache, full_cache_refresh, load_videos_cache

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh _cache/videos.json for a YouTube channel."
    )
    parser.add_argument(
        "channel",
        help="YouTube channel id (UC…), @handle, or channel URL",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Full re-fetch from YouTube (default: incremental smart refresh)",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=WORKSPACE_ROOT,
        help="Workspace root (default: parent of src/)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Parent directory for channel folders (default: <workspace>/_channels)",
    )
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Pass through to yt-dlp for playlist map refresh",
    )
    parser.add_argument(
        "--yt-dlp",
        default="yt-dlp",
        help="yt-dlp executable (default: yt-dlp)",
    )
    return parser.parse_args(argv)


def setup_channel_root(args: argparse.Namespace) -> tuple[str, str | None, str, Path]:
    channel_id, handle = resolve_channel_id(args.channel, args)
    if not handle:
        handle = resolve_channel_handle(channel_id, args)
    legacy_name = fetch_channel_name(channel_id)
    folder_name = resolve_output_folder(
        channel_id, handle, None, legacy_name, args
    )
    channel_root = (args.output_dir or channels_dir(args.workspace)) / folder_name
    channel_root.mkdir(parents=True, exist_ok=True)
    migrate_legacy_browse_cache(channel_id, channel_root, args.workspace)
    cache = load_videos_cache(channel_root)
    channel_name = (
        cache.get("channel_name") if cache and cache.get("channel_name") else legacy_name
    )
    return channel_id, handle, channel_name, channel_root


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    channel_id, handle, channel_name, channel_root = setup_channel_root(args)

    playlist_map = resolve_playlist_map(
        channel_root,
        channel_id,
        args,
        load_video_playlist_map(channel_root) is None,
    )

    if args.force:
        result = full_cache_refresh(
            channel_id,
            channel_root,
            load_videos_cache(channel_root),
            handle,
            playlist_map=playlist_map,
            reset_length_old=True,
        )
    else:
        result = ensure_video_cache(
            channel_id,
            channel_root,
            handle,
            required=None,
            playlist_map=playlist_map,
            allow_emergency_refresh=True,
        )

    print(f"Channel: {channel_name} ({channel_id})", flush=True)
    if handle:
        print(f"Handle: {handle}", flush=True)
    print(f"Videos in cache: {result.length_curr}", flush=True)
    print(f"Baseline length_old: {result.length_old}", flush=True)
    print(f"Cache file: {channel_root / '_cache' / 'videos.json'}", flush=True)
    if result.emergency_refresh:
        print("Emergency full refresh was triggered.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
