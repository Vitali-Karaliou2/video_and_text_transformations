"""Per-channel playlist cache (_cache/playlists.json) and _playlists/ folders."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from playlist_mapping import slugify_playlist_name, unique_folder_name, yt_dlp_flat_json
from project_paths import (
    browse_cache_path,
    channel_cache_dir,
    channel_playlists_dir,
    legacy_cache_path,
    playlists_cache_path,
    video_playlists_cache_path,
)


@dataclass
class PlaylistEntry:
    id: str
    title: str
    folder: str


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_playlists_cache(path: Path) -> dict | None:
    return read_json(path)


def save_playlists_cache(path: Path, payload: dict) -> None:
    write_json(path, payload)


def fetch_channel_playlist_entries(
    channel_id: str,
    run_yt_dlp: Callable[..., object],
) -> list[PlaylistEntry]:
    playlists_url = f"https://www.youtube.com/channel/{channel_id}/playlists"
    playlists_meta = yt_dlp_flat_json(run_yt_dlp, playlists_url)
    used_folders: set[str] = set()
    entries: list[PlaylistEntry] = []
    for pl in playlists_meta:
        pl_id = pl.get("id") or ""
        pl_title = pl.get("title") or "unnamed"
        folder = unique_folder_name(slugify_playlist_name(pl_title), used_folders)
        entries.append(PlaylistEntry(id=pl_id, title=pl_title, folder=folder))
    return entries


def build_playlists_payload(
    *,
    channel_id: str,
    channel_handle: str | None,
    channel_name: str | None,
    entries: list[PlaylistEntry],
) -> dict:
    return {
        "channel_id": channel_id,
        "channel_handle": channel_handle,
        "channel_name": channel_name,
        "last_checked_date": date.today().isoformat(),
        "last_checked_at": datetime.now().isoformat(timespec="seconds"),
        "playlists": [
            {"id": entry.id, "title": entry.title, "folder": entry.folder}
            for entry in entries
        ],
    }


def is_first_access_today(cached: dict | None) -> bool:
    if not cached:
        return True
    return cached.get("last_checked_date") != date.today().isoformat()


def compare_playlist_lists(
    old_playlists: list[dict],
    new_playlists: list[dict],
) -> tuple[list[dict], list[dict]]:
    old_by_id = {pl["id"]: pl for pl in old_playlists if pl.get("id")}
    new_by_id = {pl["id"]: pl for pl in new_playlists if pl.get("id")}
    added = [new_by_id[pl_id] for pl_id in new_by_id if pl_id not in old_by_id]
    removed = [old_by_id[pl_id] for pl_id in old_by_id if pl_id not in new_by_id]
    return added, removed


def known_playlist_folders(cached: dict) -> set[str]:
    return {pl["folder"] for pl in cached.get("playlists", []) if pl.get("folder")}


def resolve_misc_folder_name(playlist_folders: set[str]) -> str:
    name = "misc"
    while name in playlist_folders:
        name = f"_{name}"
    return name


def is_misc_folder(folder_name: str, playlist_folders: set[str]) -> bool:
    if folder_name in playlist_folders:
        return False
    normalized = folder_name.lstrip("_")
    return normalized == "misc"


def ensure_playlist_folders(
    channel_root: Path,
    playlists: list[dict],
    *,
    create: bool = False,
) -> list[str]:
    root = channel_playlists_dir(channel_root)
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for pl in playlists:
        folder_name = pl.get("folder")
        if not folder_name:
            continue
        folder = root / folder_name
        if not folder.is_dir():
            if create:
                folder.mkdir(parents=True, exist_ok=True)
                created.append(folder_name)
            else:
                created.append(folder_name)
    return created


def report_playlist_changes(added: list[dict], removed: list[dict]) -> None:
    for pl in added:
        print(
            f"New playlist: {pl.get('title', '?')} ({pl.get('folder', '?')})",
            flush=True,
        )
    for pl in removed:
        print(
            f"Removed playlist: {pl.get('title', '?')} ({pl.get('folder', '?')})",
            flush=True,
        )


def check_removed_playlist_references(
    removed: list[dict],
    referenced_folders: set[str] | None,
) -> None:
    if not referenced_folders:
        return
    removed_folders = {pl["folder"] for pl in removed if pl.get("folder")}
    conflict = referenced_folders & removed_folders
    if conflict:
        names = ", ".join(sorted(conflict))
        raise SystemExit(
            f"Command references playlist folder(s) removed from YouTube: {names}"
        )


def sync_channel_playlists(
    channel_root: Path,
    *,
    channel_id: str,
    channel_handle: str | None,
    channel_name: str | None,
    run_yt_dlp: Callable[..., object],
    force_fetch: bool = False,
    update_cache: bool = True,
    create_folders: bool = True,
    referenced_folders: set[str] | None = None,
) -> dict:
    cache_path = playlists_cache_path(channel_root)
    channel_cache_dir(channel_root).mkdir(parents=True, exist_ok=True)
    cached = load_playlists_cache(cache_path)

    need_fetch = force_fetch or cached is None or is_first_access_today(cached)
    if not need_fetch and cached:
        if create_folders:
            ensure_playlist_folders(channel_root, cached.get("playlists", []), create=True)
        return cached

    entries = fetch_channel_playlist_entries(channel_id, run_yt_dlp)
    payload = build_playlists_payload(
        channel_id=channel_id,
        channel_handle=channel_handle,
        channel_name=channel_name,
        entries=entries,
    )

    if cached:
        added, removed = compare_playlist_lists(
            cached.get("playlists", []),
            payload["playlists"],
        )
        if added or removed:
            report_playlist_changes(added, removed)
            check_removed_playlist_references(removed, referenced_folders)
        if not update_cache and not force_fetch:
            return cached
    else:
        print(
            f"Initializing playlist cache for {channel_handle or channel_id}...",
            flush=True,
        )

    if update_cache or cached is None or force_fetch:
        save_playlists_cache(cache_path, payload)
    if create_folders:
        created = ensure_playlist_folders(
            channel_root, payload["playlists"], create=True
        )
        if created:
            print(
                f"Created {len(created)} playlist folder(s) under _playlists/.",
                flush=True,
            )
    return payload


def validate_playlist_folders(channel_root: Path, cached: dict) -> list[str]:
    errors: list[str] = []
    playlists_root = channel_playlists_dir(channel_root)
    if not playlists_root.is_dir():
        return errors
    known = known_playlist_folders(cached)
    for child in sorted(playlists_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in known or is_misc_folder(child.name, known):
            continue
        errors.append(
            f"Folder _playlists/{child.name} is not listed in _cache/playlists.json"
        )
    return errors


def missing_playlist_folders(channel_root: Path, cached: dict) -> list[str]:
    playlists_root = channel_playlists_dir(channel_root)
    missing: list[str] = []
    for pl in cached.get("playlists", []):
        folder = pl.get("folder")
        if not folder:
            continue
        if not (playlists_root / folder).is_dir():
            missing.append(folder)
    return missing


def migrate_legacy_browse_cache(channel_id: str, channel_root: Path, workspace: Path) -> bool:
    legacy_path = legacy_cache_path(channel_id, workspace)
    new_path = browse_cache_path(channel_root)
    if not legacy_path.exists() or new_path.exists():
        return False
    data = read_json(legacy_path)
    if not data:
        return False
    data.pop("playlist_map", None)
    data.pop("playlist_map_at", None)
    write_json(new_path, data)
    legacy_path.unlink()
    print(f"Migrated browse cache to {new_path}", flush=True)
    return True


def load_video_playlist_map(channel_root: Path) -> dict[str, str] | None:
    data = read_json(video_playlists_cache_path(channel_root))
    if not data:
        return None
    mapping = data.get("map")
    return mapping if isinstance(mapping, dict) else None


def save_video_playlist_map(channel_root: Path, mapping: dict[str, str]) -> None:
    write_json(
        video_playlists_cache_path(channel_root),
        {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "map": mapping,
        },
    )


def iter_channel_roots(channels_root: Path) -> list[Path]:
    if not channels_root.is_dir():
        return []
    return sorted(
        path
        for path in channels_root.iterdir()
        if path.is_dir() and path.name.startswith("_") and path.name != "_"
    )
