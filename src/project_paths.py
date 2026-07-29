"""Shared workspace paths for yt-dlp pipeline scripts."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANNELS_DIRNAME = "_channels"
PLAYLISTS_DIRNAME = "_playlists"
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
    return channel_root / PLAYLISTS_DIRNAME


def is_channel_root(path: Path) -> bool:
    """True when path is a YouTube channel folder (has _playlists/)."""
    return path.is_dir() and channel_playlists_dir(path).is_dir()


def is_channel_container(path: Path) -> bool:
    """True when path groups channel folders but is not a channel itself."""
    return path.is_dir() and not is_channel_root(path)


def iter_channel_roots(channels_root: Path) -> list[Path]:
    """Return every YouTube channel folder under channels_root (any depth)."""
    if not channels_root.is_dir():
        return []

    found: list[Path] = []

    def walk(node: Path) -> None:
        if is_channel_root(node):
            found.append(node.resolve())
            return
        if not node.is_dir():
            return
        for child in sorted(node.iterdir()):
            if child.is_dir():
                walk(child)

    for child in sorted(channels_root.iterdir()):
        if child.is_dir():
            walk(child)
    return found


def channel_relative_ref(channel_root: Path, channels_root: Path) -> str:
    """Path of the channel folder relative to _channels/, using backslashes."""
    rel = channel_root.resolve().relative_to(channels_root.resolve())
    return rel.as_posix().replace("/", "\\")


def _split_channel_ref(ref: str) -> list[str]:
    cleaned = ref.strip().strip("\\/")
    if not cleaned:
        return []
    return [part for part in cleaned.replace("\\", "/").split("/") if part]


def resolve_channel_ref(channels_root: Path, ref: str) -> Path | None:
    """Resolve an existing channel folder from a relative ref under _channels/."""
    parts = _split_channel_ref(ref)
    if not parts:
        return None

    channels_root = channels_root.resolve()
    direct = (channels_root / Path(*parts)).resolve()
    try:
        direct.relative_to(channels_root)
    except ValueError:
        return None
    if is_channel_root(direct):
        return direct

    if len(parts) == 1:
        leaf = parts[0]
        candidates = [normalize_channel_folder_arg(leaf)]
        if candidates[0] != leaf:
            alt = (channels_root / candidates[0]).resolve()
            if is_channel_root(alt):
                return alt
        matches = [
            path
            for path in iter_channel_roots(channels_root)
            if path.name in {leaf, candidates[0]}
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def require_channel_ref(channels_root: Path, ref: str) -> Path:
    path = resolve_channel_ref(channels_root, ref)
    if path is None:
        raise FileNotFoundError(
            f"Channel folder not found under {channels_root}: {ref!r} "
            f"(expected a folder with {PLAYLISTS_DIRNAME}/)"
        )
    return path


def describe_channels_layout(channels_root: Path) -> list[str]:
    """Human-readable summary of containers and channel folders."""
    lines: list[str] = []
    if not channels_root.is_dir():
        lines.append(f"{channels_root}: not found")
        return lines

    def walk(node: Path, indent: int) -> None:
        prefix = "  " * indent
        if is_channel_root(node):
            rel = channel_relative_ref(node, channels_root)
            lines.append(f"{prefix}[channel] {rel}")
            return
        if not node.is_dir():
            return
        rel = channel_relative_ref(node, channels_root) if node != channels_root else "."
        marker = "[container]" if node != channels_root else "[root]"
        lines.append(f"{prefix}{marker} {rel}")
        if node != channels_root and channel_playlists_dir(node).exists():
            lines.append(
                f"{prefix}  WARNING: container must not contain "
                f"{PLAYLISTS_DIRNAME}/"
            )
        for child in sorted(node.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                walk(child, indent + 1)

    walk(channels_root, 0)
    channels = iter_channel_roots(channels_root)
    lines.append(f"Total channel folders: {len(channels)}")
    return lines


def find_channel_folder(
    channels_root: Path,
    handle: str | None = None,
    *,
    explicit: str | None = None,
    channel_id: str | None = None,
) -> Path | None:
    """Return an existing channel folder under channels_root, if present."""
    channels_root = channels_root.resolve()
    if explicit:
        found = resolve_channel_ref(channels_root, explicit)
        if found:
            return found

    candidates: list[str] = []
    if handle:
        candidates.append(channel_folder_name(handle))
    seen: set[str] = set()
    for folder_name in candidates:
        if folder_name in seen:
            continue
        seen.add(folder_name)
        for path in iter_channel_roots(channels_root):
            if path.name == folder_name:
                return path

    if channel_id:
        for path in iter_channel_roots(channels_root):
            for cache_name in ("playlists.json", "videos.json"):
                cache_path = path / "_cache" / cache_name
                try:
                    data = json.loads(cache_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if data.get("channel_id") == channel_id:
                    return path
    return None


def browse_cache_path(channel_root: Path) -> Path:
    return channel_cache_dir(channel_root) / "browse.json"


def videos_cache_path(channel_root: Path) -> Path:
    return channel_cache_dir(channel_root) / "videos.json"


def playlists_cache_path(channel_root: Path) -> Path:
    return channel_cache_dir(channel_root) / "playlists.json"


def video_playlists_cache_path(channel_root: Path) -> Path:
    return channel_cache_dir(channel_root) / "video_playlists.json"


def channel_summaries_dir(channel_root: Path, day: date | None = None) -> Path:
    folder_day = (day or date.today()).isoformat()
    return channel_root / "_summaries" / folder_day


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
