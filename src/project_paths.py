"""Shared workspace paths for yt-dlp pipeline scripts."""

from __future__ import annotations

import re
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANNELS_DIRNAME = "_channels"
LEGACY_CACHE_DIRNAME = "cache"
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def channels_dir(workspace: Path | None = None) -> Path:
    return (workspace or WORKSPACE_ROOT) / DEFAULT_CHANNELS_DIRNAME


def legacy_cache_dir(workspace: Path | None = None) -> Path:
    return (workspace or WORKSPACE_ROOT) / LEGACY_CACHE_DIRNAME


def legacy_cache_path(channel_id: str, workspace: Path | None = None) -> Path:
    return legacy_cache_dir(workspace) / f"{channel_id}.json"


def channel_cache_dir(channel_root: Path) -> Path:
    return channel_root / "_cache"


def channel_playlists_dir(channel_root: Path) -> Path:
    return channel_root / "_playlists"


def browse_cache_path(channel_root: Path) -> Path:
    return channel_cache_dir(channel_root) / "browse.json"


def playlists_cache_path(channel_root: Path) -> Path:
    return channel_cache_dir(channel_root) / "playlists.json"


def video_playlists_cache_path(channel_root: Path) -> Path:
    return channel_cache_dir(channel_root) / "video_playlists.json"


def sanitize_handle_for_path(handle: str) -> str:
    name = handle.strip()
    if name.startswith("@"):
        name = name[1:]
    name = name.replace(" ", "_")
    name = INVALID_PATH_CHARS.sub("", name)
    return name.strip(" .")


def channel_folder_name(handle: str) -> str:
    """Folder name under _channels/ derived from the channel @handle."""
    name = sanitize_handle_for_path(handle)
    if not name:
        return "_unnamed_channel"
    return name if name.startswith("_") else f"_{name}"


def export_file_basename(handle: str) -> str:
    """Base name for TXT/XLSX exports (no leading underscore)."""
    folder = channel_folder_name(handle)
    return folder[1:] if folder.startswith("_") else folder


def normalize_channel_folder_arg(name: str) -> str:
    """Normalize an explicit folder argument to _Handle form."""
    cleaned = sanitize_handle_for_path(name.lstrip("_"))
    if not cleaned:
        raise ValueError("Invalid channel folder name")
    return f"_{cleaned}"


def find_channel_folder(
    channels_root: Path,
    handle: str | None = None,
    *,
    explicit: str | None = None,
) -> Path | None:
    """Return an existing channel folder under channels_root, if present."""
    candidates: list[str] = []
    if explicit:
        candidates.append(normalize_channel_folder_arg(explicit))
    if handle:
        candidates.append(channel_folder_name(handle))
    seen: set[str] = set()
    for folder_name in candidates:
        if folder_name in seen:
            continue
        seen.add(folder_name)
        path = channels_root / folder_name
        if path.is_dir():
            return path
    return None
