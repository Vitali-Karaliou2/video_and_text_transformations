"""Per-channel playlist cache (_cache/playlists.json) and _playlists/ folders."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from channels.playlist_mapping import (
    channel_context,
    channel_playlists_json,
    former_folder_name,
    seconds_to_duration_text,
    slugify_playlist_name,
    unique_folder_name,
)
from shared.project_paths import (
    browse_cache_path,
    channel_cache_dir,
    channel_playlists_dir,
    iter_channel_roots,
    legacy_cache_path,
    playlists_cache_path,
    unlisted_playlists_dir,
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
    playlists_meta = channel_playlists_json(run_yt_dlp, channel_id)
    used_folders: set[str] = set()
    entries: list[PlaylistEntry] = []
    context = channel_context(playlists_meta)
    for pl in playlists_meta:
        pl_id = pl.get("id") or ""
        pl_title = pl.get("title") or "unnamed"
        folder = unique_folder_name(
            slugify_playlist_name(pl_title, context=context), used_folders
        )
        entries.append(PlaylistEntry(id=pl_id, title=pl_title, folder=folder))
    return entries


def ensure_playlist_aliases(cached: dict) -> bool:
    playlists = cached.get("playlists", [])
    if not playlists:
        return False
    if all(pl.get("alias") for pl in playlists):
        return False
    assign_playlist_aliases(playlists)
    cached["playlists"] = playlists
    return True


def resolve_playlist_selection(scope: str, cached: dict) -> dict:
    playlists = cached.get("playlists", [])
    token = scope.strip()
    if token.startswith("#"):
        for playlist in playlists:
            if playlist.get("alias", "").lower() == token.lower():
                return playlist
        raise SystemExit(f"Unknown playlist alias: {token}")
    if len(token) < 3:
        raise SystemExit("Playlist name prefix must be at least 3 characters.")
    matches = [
        pl
        for pl in playlists
        if pl.get("title", "").lower().startswith(token.lower())
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"No playlist matches prefix: {token!r}")
    titles = ", ".join(pl["title"] for pl in matches[:5])
    raise SystemExit(
        f"Ambiguous playlist prefix {token!r}; use alias. Matches include: {titles}"
    )


def playlist_order(cached: dict) -> list[dict]:
    return list(cached.get("playlists", []))


def index_to_excel_alias(index: int) -> str:
    """0 -> #A, 25 -> #Z, 26 -> #AA (Excel-style column letters)."""
    n = index + 1
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"#{letters}"


def assign_playlist_aliases(playlists: list[dict]) -> list[dict]:
    for index, playlist in enumerate(playlists):
        playlist["alias"] = index_to_excel_alias(index)
    return playlists


def build_playlists_payload(
    *,
    channel_id: str,
    channel_handle: str | None,
    channel_name: str | None,
    entries: list[PlaylistEntry],
) -> dict:
    playlists = [
        {"id": entry.id, "title": entry.title, "folder": entry.folder}
        for entry in entries
    ]
    assign_playlist_aliases(playlists)
    return {
        "channel_id": channel_id,
        "channel_handle": channel_handle,
        "channel_name": channel_name,
        "last_checked_date": date.today().isoformat(),
        "last_checked_at": datetime.now().isoformat(timespec="seconds"),
        "playlists": playlists,
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


UNLISTED_SOURCE = "unlisted"
VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")
VIDEO_URL_RE = re.compile(
    r"(?:v=|youtu\.be/|/shorts/|/live/)([\w-]{11})"
)


def is_unlisted_playlist(playlist: dict) -> bool:
    return playlist.get("source") == UNLISTED_SOURCE


def youtube_playlists_only(playlists: list[dict]) -> list[dict]:
    return [pl for pl in playlists if not is_unlisted_playlist(pl)]


def title_from_unlisted_folder(folder: str) -> str:
    """AI_Capabilities_and_Limitations -> AI Capabilities and Limitations."""
    return folder.replace("_", " ").strip() or folder


def unlisted_playlist_id(folder: str) -> str:
    return f"{UNLISTED_SOURCE}:{folder}"


def video_id_from_ref(text: str) -> str | None:
    """A watch URL or a bare 11-character id; blank and comment lines skip."""
    line = text.strip()
    if not line or line.startswith("#"):
        return None
    if VIDEO_ID_RE.fullmatch(line):
        return line
    match = VIDEO_URL_RE.search(line)
    return match.group(1) if match else None


def read_unlisted_video_ids(path: Path) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        vid = video_id_from_ref(line)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        ids.append(vid)
    return ids


def load_unlisted_playlist_files(channel_root: Path) -> list[dict]:
    """Playlists declared under _playlists_unlisted/<folder>.txt.

    The file stem is the folder name under _playlists/; the title is that
    name with underscores turned into spaces. Each non-empty line is a
    watch URL or a bare video id, in the order they should appear.
    """
    root = unlisted_playlists_dir(channel_root)
    if not root.is_dir():
        return []
    entries: list[dict] = []
    for path in sorted(root.glob("*.txt")):
        folder = path.stem.strip()
        if not folder:
            continue
        video_ids = read_unlisted_video_ids(path)
        if not video_ids:
            print(
                f"WARNING: unlisted playlist {path.name} has no video ids; "
                "skipped.",
                flush=True,
            )
            continue
        entries.append(
            {
                "id": unlisted_playlist_id(folder),
                "title": title_from_unlisted_folder(folder),
                "folder": folder,
                "source": UNLISTED_SOURCE,
                "video_ids": video_ids,
            }
        )
    return entries


def merge_unlisted_playlists(
    playlists: list[dict], channel_root: Path
) -> tuple[list[dict], bool]:
    """Keep YouTube playlists; replace hand-written unlisted ones from files."""
    youtube = youtube_playlists_only(playlists)
    used = {pl.get("folder") for pl in youtube if pl.get("folder")}
    unlisted = load_unlisted_playlist_files(channel_root)
    merged: list[dict] = list(youtube)
    for entry in unlisted:
        folder = entry["folder"]
        if folder in used:
            print(
                f"WARNING: unlisted playlist folder {folder!r} collides "
                "with a YouTube playlist; skipped.",
                flush=True,
            )
            continue
        used.add(folder)
        merged.append(
            {
                "id": entry["id"],
                "title": entry["title"],
                "folder": folder,
                "source": UNLISTED_SOURCE,
            }
        )
    before = [
        (pl.get("id"), pl.get("folder"), pl.get("title"), pl.get("source"))
        for pl in playlists
    ]
    after = [
        (pl.get("id"), pl.get("folder"), pl.get("title"), pl.get("source"))
        for pl in merged
    ]
    return merged, before != after


def fetch_unlisted_video_detail(
    video_id: str,
    run_yt_dlp: Callable[..., object],
) -> dict:
    """Title and duration for one unlisted video (by watch URL)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    proc = run_yt_dlp(
        "--skip-download",
        "--print",
        "%(.{id,title,duration})j",
        url,
    )
    if getattr(proc, "returncode", 1) != 0:
        err = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
        raise RuntimeError(
            f"yt-dlp failed for unlisted video {video_id}:\n{err[:2000]}"
        )
    line = (getattr(proc, "stdout", "") or "").strip().splitlines()
    if not line:
        raise RuntimeError(f"yt-dlp returned no metadata for {video_id}")
    info = json.loads(line[0])
    return {
        "title": info.get("title") or video_id,
        "duration_text": seconds_to_duration_text(info.get("duration")),
        "url": url,
    }


def apply_unlisted_to_video_map(
    channel_root: Path,
    mapping: dict[str, str],
    details: dict[str, dict],
    run_yt_dlp: Callable[..., object] | None = None,
) -> tuple[dict[str, str], dict[str, dict], int]:
    """Map every video listed under _playlists_unlisted/ to its series.

    Unlisted course videos never appear in the channel Videos feed, so the
    hand-written list is the only place they show up; missing titles are
    filled via yt-dlp when a runner is given.
    """
    added = 0
    mapping = dict(mapping)
    details = {vid: dict(meta) for vid, meta in details.items()}
    for entry in load_unlisted_playlist_files(channel_root):
        title = entry["title"]
        for index, video_id in enumerate(entry["video_ids"], start=1):
            if mapping.get(video_id) != title:
                added += 1
            mapping[video_id] = title
            prev = details.get(video_id) or {}
            meta = {
                "title": prev.get("title") or video_id,
                "duration_text": prev.get("duration_text"),
                "url": prev.get("url")
                or f"https://www.youtube.com/watch?v={video_id}",
                "playlist_index": index,
            }
            if (
                run_yt_dlp is not None
                and (meta["title"] == video_id or not meta["duration_text"])
            ):
                try:
                    fetched = fetch_unlisted_video_detail(video_id, run_yt_dlp)
                except RuntimeError as exc:
                    print(f"WARNING: {exc}", flush=True)
                else:
                    meta["title"] = fetched["title"] or meta["title"]
                    meta["duration_text"] = (
                        fetched["duration_text"] or meta["duration_text"]
                    )
                    meta["url"] = fetched["url"]
            details[video_id] = meta
    return mapping, details, added


def rename_folder_of_an_older_rule(
    root: Path, pl: dict, wanted: set[str]
) -> bool:
    """Move a playlist that was foldered under an earlier naming rule.

    Only C# and its kin are spelled out now where they used to be cut
    down to a letter, so this fires once per such playlist: without it
    the downloads stay in "advanced_c" while everything after this call
    looks into an empty "advanced_c_sharp".
    """
    folder_name = pl.get("folder") or ""
    former = former_folder_name(pl.get("title") or "")
    if former in (folder_name, "") or former in wanted:
        return False
    source = root / former
    if not source.is_dir():
        return False
    try:
        source.rename(root / folder_name)
    except OSError as exc:
        print(
            f"WARNING: playlist folder {former} could not be renamed to "
            f"{folder_name}: {exc}",
            flush=True,
        )
        return False
    print(f"Renamed playlist folder: {former} -> {folder_name}", flush=True)
    return True


def ensure_playlist_folders(
    channel_root: Path,
    playlists: list[dict],
    *,
    create: bool = False,
) -> list[str]:
    root = channel_playlists_dir(channel_root)
    root.mkdir(parents=True, exist_ok=True)
    wanted = {pl["folder"] for pl in playlists if pl.get("folder")}
    created: list[str] = []
    if not wanted:
        # A channel that keeps no playlists still needs the one folder its
        # videos go to - the same misc/ that holds whatever falls outside
        # a playlist elsewhere. Left to itself, _playlists/ would stay
        # empty and every later step would have nowhere to put a video.
        misc = root / resolve_misc_folder_name(set())
        if not misc.is_dir():
            if create:
                misc.mkdir(parents=True, exist_ok=True)
            created.append(misc.name)
    for pl in playlists:
        folder_name = pl.get("folder")
        if not folder_name:
            continue
        folder = root / folder_name
        if folder.is_dir():
            continue
        if create and rename_folder_of_an_older_rule(root, pl, wanted):
            continue
        if create:
            folder.mkdir(parents=True, exist_ok=True)
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
    create_folders: bool = False,
    referenced_folders: set[str] | None = None,
) -> dict:
    cache_path = playlists_cache_path(channel_root)
    channel_cache_dir(channel_root).mkdir(parents=True, exist_ok=True)
    cached = load_playlists_cache(cache_path)

    need_fetch = force_fetch or cached is None or is_first_access_today(cached)
    if not need_fetch and cached:
        playlists, unlisted_changed = merge_unlisted_playlists(
            cached.get("playlists", []), channel_root
        )
        cached["playlists"] = playlists
        alias_changed = ensure_playlist_aliases(cached)
        if unlisted_changed or alias_changed:
            save_playlists_cache(cache_path, cached)
            if unlisted_changed:
                print(
                    "Merged hand-written playlists from _playlists_unlisted/.",
                    flush=True,
                )
        if create_folders:
            ensure_playlist_folders(
                channel_root, cached.get("playlists", []), create=True
            )
        else:
            unlisted = [
                pl
                for pl in cached.get("playlists", [])
                if is_unlisted_playlist(pl)
            ]
            if unlisted:
                ensure_playlist_folders(channel_root, unlisted, create=True)
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
            youtube_playlists_only(cached.get("playlists", [])),
            youtube_playlists_only(payload["playlists"]),
        )
        if added or removed:
            report_playlist_changes(added, removed)
            check_removed_playlist_references(removed, referenced_folders)
        if not update_cache and not force_fetch:
            playlists, _ = merge_unlisted_playlists(
                cached.get("playlists", []), channel_root
            )
            cached["playlists"] = playlists
            return cached
    else:
        print(
            f"Initializing playlist cache for {channel_handle or channel_id}...",
            flush=True,
        )

    playlists, unlisted_changed = merge_unlisted_playlists(
        payload["playlists"], channel_root
    )
    payload["playlists"] = playlists
    if update_cache or cached is None or force_fetch:
        assign_playlist_aliases(payload["playlists"])
        save_playlists_cache(cache_path, payload)
        if unlisted_changed:
            print(
                "Merged hand-written playlists from _playlists_unlisted/.",
                flush=True,
            )
    if create_folders:
        created = ensure_playlist_folders(
            channel_root, payload["playlists"], create=True
        )
        if created:
            print(
                f"Created {len(created)} folder(s) under _playlists/: "
                f"{', '.join(created)}.",
                flush=True,
            )
    else:
        # Hand-written unlisted series are few and intentional - make their
        # folders now. YouTube playlists stay deferred (there may be hundreds).
        unlisted = [
            pl for pl in payload["playlists"] if is_unlisted_playlist(pl)
        ]
        if unlisted:
            created = ensure_playlist_folders(
                channel_root, unlisted, create=True
            )
            if created:
                print(
                    f"Created {len(created)} unlisted playlist folder(s): "
                    f"{', '.join(created)}.",
                    flush=True,
                )
        missing = [
            pl["folder"]
            for pl in youtube_playlists_only(payload["playlists"])
            if pl.get("folder")
            and not (channel_playlists_dir(channel_root) / pl["folder"]).is_dir()
        ]
        if missing:
            print(
                f"Playlist folders not created ({len(missing)} pending under "
                "_playlists/). They appear when a video of that playlist is "
                "transcribed or downloaded; pass --create-playlist-folders "
                "to make them all now.",
                flush=True,
            )
    if not youtube_playlists_only(payload["playlists"]) and not any(
        is_unlisted_playlist(pl) for pl in payload["playlists"]
    ):
        print(
            "This channel keeps no playlists; its videos go to "
            f"_playlists/{resolve_misc_folder_name(set())}/ "
            "(created when the first video is transcribed).",
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


def load_video_playlist_details(channel_root: Path) -> dict[str, dict] | None:
    data = read_json(video_playlists_cache_path(channel_root))
    if not data:
        return None
    details = data.get("details")
    return details if isinstance(details, dict) else None


def save_video_playlist_map(
    channel_root: Path,
    mapping: dict[str, str],
    details: dict[str, dict] | None = None,
) -> None:
    payload: dict = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "map": mapping,
    }
    if details is not None:
        payload["details"] = details
    write_json(video_playlists_cache_path(channel_root), payload)
