#!/usr/bin/env python3
"""What to work on next, read off the channel caches and off YouTube.

A job is one video: its id, title, duration and the folder its results
belong in. Both the downloader and the remote transcription start from such
a list, whether it comes from a playlist on YouTube, from a hand-written
series under _playlists_unlisted/, or from the channel-wide video cache.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from shared.sessions import run_tool
from shared.yt_dlp_opts import (
    YOUTUBE_REMOTE_COMPONENTS,
    YOUTUBE_SYSTEM_CERTS,
    cookie_args,
)

from channels.playlist_mapping import playlist_only_browse_entries


def video_id_from_url(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/)([\w-]{11})", url)
    if not match:
        raise SystemExit(f"Cannot extract a video id from URL: {url}")
    return match.group(1)


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def duration_to_text(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def duration_text_to_seconds(text: object) -> float | None:
    raw = str(text or "").strip()
    if not raw or "?" in raw:
        return None
    parts = raw.split(":")
    try:
        nums = [int(part) for part in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
    if len(nums) == 2:
        return float(nums[0] * 60 + nums[1])
    if len(nums) == 1:
        return float(nums[0])
    return None


def read_json_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def yt_dlp_json(
    args_list: list[str],
    *,
    cookies: Path | None = None,
    cookies_from_browser: str | None = None,
) -> dict:
    result = run_tool([
        sys.executable,
        "-m",
        "yt_dlp",
        *YOUTUBE_SYSTEM_CERTS,
        *YOUTUBE_REMOTE_COMPONENTS,
        *cookie_args(cookies, cookies_from_browser),
        *args_list,
    ])
    if result.returncode != 0:
        raise SystemExit(f"yt-dlp failed:\n{result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise SystemExit("yt-dlp returned invalid JSON")


def fetch_video_info(
    url: str,
    *,
    cookies: Path | None = None,
    cookies_from_browser: str | None = None,
) -> dict:
    return yt_dlp_json(
        ["--no-playlist", "-J", url],
        cookies=cookies,
        cookies_from_browser=cookies_from_browser,
    )


def fetch_playlist_entries(
    playlist_id: str,
    *,
    cookies: Path | None = None,
    cookies_from_browser: str | None = None,
) -> list[dict]:
    """(id, title, duration, index, url) per entry, in playlist order."""
    data = yt_dlp_json(
        ["--flat-playlist", "-J",
         f"https://www.youtube.com/playlist?list={playlist_id}"],
        cookies=cookies,
        cookies_from_browser=cookies_from_browser,
    )
    entries = []
    for index, entry in enumerate(data.get("entries") or [], start=1):
        if entry and entry.get("id"):
            entries.append(
                {
                    "id": entry["id"],
                    "title": str(entry.get("title") or "").strip(),
                    "duration": entry.get("duration"),
                    "index": index,
                    "url": watch_url(entry["id"]),
                }
            )
    return entries


def load_playlist_meta(channel_dir: Path, playlist_folder: str) -> dict:
    """YouTube playlist (id, title) behind a local playlist folder, plus the
    channel name - resolved via _cache/playlists.json (built by the summary
    scripts)."""
    cache_path = channel_dir / "_cache" / "playlists.json"
    data = read_json_file(cache_path)
    if data is None:
        raise SystemExit(
            f"Playlist cache not found or invalid: {cache_path}\n"
            "Run the summary script for this channel first."
        )
    for playlist in data.get("playlists") or []:
        if playlist.get("folder") == playlist_folder:
            return {
                "id": playlist.get("id") or "",
                "title": playlist.get("title") or playlist_folder,
                "channel_name": data.get("channel_name") or "",
            }
    raise SystemExit(
        f"Playlist folder {playlist_folder!r} not found in {cache_path}"
    )


def load_unlisted_playlist_entries(
    channel_dir: Path, playlist_folder: str
) -> list[dict]:
    """Entries of a series hand-written in _playlists_unlisted/."""
    from channels.channel_playlists import (
        load_unlisted_playlist_files,
        load_video_playlist_details,
    )

    for entry in load_unlisted_playlist_files(channel_dir):
        if entry["folder"] != playlist_folder:
            continue
        details = load_video_playlist_details(channel_dir) or {}
        jobs: list[dict] = []
        for index, video_id in enumerate(entry["video_ids"], start=1):
            meta = details.get(video_id) or {}
            jobs.append(
                {
                    "id": video_id,
                    "title": str(meta.get("title") or "").strip(),
                    "duration": duration_text_to_seconds(
                        meta.get("duration_text")
                    ),
                    "index": index,
                    "url": meta.get("url") or watch_url(video_id),
                }
            )
        return jobs
    raise SystemExit(
        f"Unlisted playlist folder {playlist_folder!r} has no matching "
        f"file under {channel_dir / '_playlists_unlisted'}"
    )


def load_channel_flat_jobs(channel_dir: Path) -> tuple[list[dict], str]:
    """Channel-wide flat job list (newest first) from the summary caches:
    _cache/videos.json (video order), _cache/video_playlists.json (video ->
    playlist title) and _cache/playlists.json (playlist title -> folder).
    Videos outside any playlist go to the misc/ folder.

    Playlist-only videos (in _cache/video_playlists.json but not in the
    Videos-tab cache) are appended too, so flat transcription and
    transcribe_and_edit_next_by_substr see the same set as a summary built
    with --include-playlist-only."""
    cache = read_json_file(channel_dir / "_cache" / "videos.json")
    if not cache or not cache.get("videos"):
        raise SystemExit(
            f"Video cache not found or empty: "
            f"{channel_dir / '_cache' / 'videos.json'}\n"
            "Run the summary script for this channel first."
        )
    channel_name = str(cache.get("channel_name") or "")

    playlists_data = (
        read_json_file(channel_dir / "_cache" / "playlists.json") or {}
    )
    folder_by_title: dict[str, str] = {}
    id_by_title: dict[str, str] = {}
    for playlist in playlists_data.get("playlists") or []:
        title = playlist.get("title") or ""
        folder_by_title[title] = playlist.get("folder") or ""
        id_by_title[title] = playlist.get("id") or ""
    misc_folder = "misc"
    while misc_folder in set(folder_by_title.values()):
        misc_folder = f"_{misc_folder}"

    vp_data = (
        read_json_file(channel_dir / "_cache" / "video_playlists.json") or {}
    )
    video_map = vp_data.get("map") or {}
    details = vp_data.get("details") or {}

    jobs: list[dict] = []
    for video in cache["videos"]:
        video_id = video.get("id")
        if not video_id:
            continue
        pl_title = str(video_map.get(video_id) or "")
        detail = details.get(video_id) or {}
        jobs.append(
            {
                "id": video_id,
                "title": str(video.get("title") or "").strip(),
                "duration": None,
                "index": int(detail.get("playlist_index") or 0),
                "url": watch_url(video_id),
                "folder": folder_by_title.get(pl_title) or misc_folder,
                "playlist_meta": {
                    "id": id_by_title.get(pl_title, ""),
                    "title": pl_title,
                    "channel_name": channel_name,
                },
            }
        )
    known_ids = {job["id"] for job in jobs}
    extras = playlist_only_browse_entries(video_map, details, known_ids)
    for video in extras:
        video_id = video["id"]
        pl_title = str(video_map.get(video_id) or "")
        detail = details.get(video_id) or {}
        jobs.append(
            {
                "id": video_id,
                "title": str(video.get("title") or "").strip(),
                "duration": duration_text_to_seconds(
                    video.get("duration_text")
                ),
                "index": int(detail.get("playlist_index") or 0),
                "url": video.get("url") or watch_url(video_id),
                "folder": folder_by_title.get(pl_title) or misc_folder,
                "playlist_meta": {
                    "id": id_by_title.get(pl_title, ""),
                    "title": pl_title,
                    "channel_name": channel_name,
                },
            }
        )
    if extras:
        print(
            f"Included {len(extras)} playlist-only video(s) from the cache "
            f"(not in the channel Videos feed).",
            flush=True,
        )
    return jobs, channel_name
