#!/usr/bin/env python3
"""The video files of a playlist, and the names they may be given.

Every stage after the download works on the same folder of media files and
has to agree on what counts as one, how a file made from a YouTube video is
named, and how long that name may be before Windows refuses the path.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v",
    ".mp3", ".m4a", ".wav", ".opus", ".ogg", ".flac",
}
# Video metadata (title, date, duration, chapters) saved in remote mode for
# the final-editing stage (document title and timecode-based ToC).
INFO_DIRNAME = "INFO"
WINDOWS_MAX_PATH = 260
# With long paths switched on the OS allows 32767, but the .docx is still
# handed to WPS Writer / Word for the PDF pass and those are not reliably
# long-path aware, so stay roomy rather than unlimited.
LONG_PATH_LIMIT = 400


def list_videos(playlist_dir: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in playlist_dir.iterdir()
            if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
        ),
        key=lambda path: path.name.lower(),
    )


def local_media_by_id(playlist_dir: Path) -> dict[str, Path]:
    """Local media files keyed by the [video id] marker in their names."""
    by_id: dict[str, Path] = {}
    for video in list_videos(playlist_dir):
        match = re.search(r"\[([\w-]{11})\]$", video.stem)
        if match:
            by_id[match.group(1)] = video
    return by_id


@lru_cache(maxsize=1)
def long_paths_enabled() -> bool:
    """Whether Windows is set to accept paths longer than MAX_PATH."""
    if sys.platform != "win32":
        return True
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    except OSError:
        return False
    return bool(value)


def path_limit() -> int:
    return LONG_PATH_LIMIT if long_paths_enabled() else WINDOWS_MAX_PATH


def stem_budget(playlist_dir: Path) -> int:
    """How long a file stem may be before Windows refuses the path.

    Unless long paths are enabled system-wide, a full path may not exceed
    MAX_PATH; the longest file built from a stem is the edited document,
    <playlist>/<LANG>/OUTPUT/<stem>.docx (see docs/create_final_docs.py).
    """
    longest_suffix = len("\\RU\\OUTPUT\\") + len(".docx")
    return path_limit() - 1 - len(str(playlist_dir)) - longest_suffix


def remote_stem(
    index: int, title: str, video_id: str, max_len: int | None = None
) -> str:
    """Result-file stem in the local naming convention:
    NN_<sanitized title> [<id>], with the title cut to fit `max_len`."""
    try:
        from yt_dlp.utils import sanitize_filename

        clean = sanitize_filename(title) if title else video_id
    except ImportError:
        clean = re.sub(r'[\\/:*?"<>|]', "_", title) if title else video_id
    prefix = f"{index:02d}_" if index else ""
    marker = f" [{video_id}]"
    if max_len is not None:
        room = max_len - len(prefix) - len(marker)
        if room < len(clean):
            clean = clean[: max(1, room)].rstrip(" .,;-")
    return f"{prefix}{clean}{marker}"
