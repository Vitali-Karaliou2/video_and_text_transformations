"""Per-channel video cache (_cache/videos.json) with smart refresh and --new numbering."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from channels.channel_browse import (
    browse_api_header,
    fetch_all_browse_videos,
    fetch_browse_up_to_count,
    fetch_channel_name,
    fetch_first_browse_page,
    new_videos_at_head,
)
from channels.channel_playlists import read_json, write_json
from shared.project_paths import browse_cache_path, videos_cache_path

MISC_PLAYLIST = "misc"
CACHE_STALE_HOURS = 24
EMERGENCY_NOTICE = (
    "Emergency full cache refresh was performed; the requested export was not completed. "
    "Re-run the command after the cache has been rebuilt."
)


@dataclass
class VideoCacheResult:
    videos: list[dict]
    channel_name: str
    cache_used: bool
    length_curr: int
    length_old: int
    emergency_refresh: bool
    emergency_message: str | None = None


def list_pos_to_display_number(pos: int, length_curr: int, length_old: int) -> int:
    """Map 0-based list position to stable display number (<=0 for new head videos)."""
    neg_from_end = -(length_curr - pos)
    return neg_from_end + (length_old + 1)


def display_number_to_list_pos(num: int, length_curr: int, length_old: int) -> int:
    neg_from_end = num - (length_old + 1)
    return length_curr + neg_from_end


def is_new_display_number(num: int) -> bool:
    return num <= 0


def playlist_key(title: str | None) -> str:
    return title if title else MISC_PLAYLIST


def empty_playlist_lengths() -> dict[str, dict[str, int]]:
    return {}


def compute_playlist_lengths(
    videos: list[dict],
    playlist_map: dict[str, str],
) -> dict[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for video in videos:
        key = playlist_key(playlist_map.get(video["id"], ""))
        counts[key] = counts.get(key, 0) + 1
    return {name: {"length_curr": count, "length_old": count} for name, count in counts.items()}


def sync_playlist_lengths(
    lengths: dict[str, dict[str, int]],
    videos: list[dict],
    playlist_map: dict[str, str],
) -> dict[str, dict[str, int]]:
    """Recompute length_curr per playlist; preserve length_old where present."""
    counts: dict[str, int] = {}
    for video in videos:
        key = playlist_key(playlist_map.get(video["id"], ""))
        counts[key] = counts.get(key, 0) + 1
    updated: dict[str, dict[str, int]] = {}
    for name, count in counts.items():
        old_entry = lengths.get(name, {})
        length_old = int(old_entry.get("length_old", count))
        updated[name] = {"length_curr": count, "length_old": length_old}
    return updated


def bump_playlist_lengths_for_new_videos(
    lengths: dict[str, dict[str, int]],
    new_videos: list[dict],
    playlist_map: dict[str, str],
) -> dict[str, dict[str, int]]:
    updated = {name: dict(entry) for name, entry in lengths.items()}
    for video in new_videos:
        key = playlist_key(playlist_map.get(video["id"], ""))
        entry = updated.setdefault(key, {"length_curr": 0, "length_old": 0})
        entry["length_curr"] = int(entry.get("length_curr", 0)) + 1
    return updated


def commit_length_old(cache: dict, *, use_new: bool) -> None:
    if use_new:
        return
    length_curr = len(cache.get("videos", []))
    cache["length_old"] = length_curr
    cache["length_curr"] = length_curr
    for entry in cache.get("playlist_lengths", {}).values():
        entry["length_old"] = entry.get("length_curr", entry.get("length_old", 0))


def migrate_browse_to_videos(channel_root: Path) -> bool:
    browse_path = browse_cache_path(channel_root)
    videos_path = videos_cache_path(channel_root)
    if not browse_path.exists() or videos_path.exists():
        return False
    data = read_json(browse_path)
    if not data or not data.get("videos"):
        return False
    count = len(data["videos"])
    payload = {
        "channel_id": data.get("channel_id"),
        "channel_name": data.get("channel_name"),
        "channel_handle": data.get("channel_handle"),
        "length_curr": count,
        "length_old": count,
        "last_validated_at": data.get("last_fetched_at"),
        "last_fetched_at": data.get("last_fetched_at"),
        "continuation_token": data.get("continuation_token"),
        "pages_fetched": data.get("pages_fetched"),
        "videos": data["videos"],
        "playlist_lengths": empty_playlist_lengths(),
    }
    write_json(videos_path, payload)
    print(f"Migrated browse cache to {videos_path}", flush=True)
    return True


def load_videos_cache(channel_root: Path) -> dict | None:
    migrate_browse_to_videos(channel_root)
    return read_json(videos_cache_path(channel_root))


def save_videos_cache(channel_root: Path, cache: dict) -> None:
    videos = cache.get("videos", [])
    cache["length_curr"] = len(videos)
    cache["last_fetched_at"] = datetime.now().isoformat(timespec="seconds")
    write_json(videos_cache_path(channel_root), cache)


def cache_is_complete(cache: dict) -> bool:
    return bool(cache.get("videos")) and cache.get("continuation_token") is None


def cache_is_stale(cache: dict) -> bool:
    stamp = cache.get("last_validated_at") or cache.get("last_fetched_at")
    if not stamp:
        return True
    try:
        validated = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return datetime.now() - validated > timedelta(hours=CACHE_STALE_HOURS)


def validation_sample_indices(length: int) -> list[int]:
    if length < 2:
        return [0] if length else []
    if length == 2:
        return [0, 1]
    candidates = list(range(1, length - 1))
    random.shuffle(candidates)
    picks = candidates[:2] if len(candidates) >= 2 else candidates[:1]
    indices = [0, length - 1, *picks]
    return sorted(set(indices))


def videos_match(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    return a.get("id") == b.get("id") and a.get("title") == b.get("title")


def validate_cache_samples(
    live: list[dict],
    cached: list[dict],
    *,
    live_offset: int = 0,
) -> bool:
    if len(cached) < 2 or len(live) < live_offset + len(cached):
        return False
    for cached_index in validation_sample_indices(len(cached)):
        live_index = live_offset + cached_index
        if live_index >= len(live):
            return False
        if not videos_match(live[live_index], cached[cached_index]):
            return False
    return True


def merge_new_with_cache(new_videos: list[dict], cached_videos: list[dict]) -> list[dict]:
    ordered: list[dict] = []
    seen: set[str] = set()
    for video in new_videos:
        if video["id"] not in seen:
            ordered.append(video)
            seen.add(video["id"])
    for video in cached_videos:
        if video["id"] not in seen:
            ordered.append(video)
            seen.add(video["id"])
    return ordered


def extend_target_count(required: int | None, cached_len: int, *, complete: bool) -> int | None:
    """How many videos to fetch while extending a partial cache."""
    if required is None:
        return None
    if complete:
        return required
    return max(required, cached_len)


def full_cache_refresh(
    channel_id: str,
    channel_root: Path,
    cache: dict | None,
    handle: str | None,
    *,
    playlist_map: dict[str, str] | None,
    reset_length_old: bool = True,
) -> VideoCacheResult:
    print("Performing full cache refresh...", flush=True)
    videos, channel_name, next_token, pages_fetched = fetch_all_browse_videos(channel_id)
    length = len(videos)
    payload = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "channel_handle": handle or (cache.get("channel_handle") if cache else None),
        "length_curr": length,
        "length_old": length if reset_length_old else int((cache or {}).get("length_old", length)),
        "last_validated_at": datetime.now().isoformat(timespec="seconds"),
        "last_fetched_at": datetime.now().isoformat(timespec="seconds"),
        "continuation_token": next_token,
        "pages_fetched": pages_fetched,
        "videos": videos,
        "playlist_lengths": (
            compute_playlist_lengths(videos, playlist_map or {})
            if playlist_map is not None
            else empty_playlist_lengths()
        ),
    }
    if playlist_map is not None and not reset_length_old and cache:
        old_lengths = cache.get("playlist_lengths", {})
        for name, entry in payload["playlist_lengths"].items():
            if name in old_lengths:
                entry["length_old"] = old_lengths[name].get("length_old", entry["length_curr"])
    save_videos_cache(channel_root, payload)
    return VideoCacheResult(
        videos=videos,
        channel_name=channel_name,
        cache_used=False,
        length_curr=length,
        length_old=payload["length_old"],
        emergency_refresh=False,
    )


def emergency_full_refresh(
    channel_id: str,
    channel_root: Path,
    cache: dict | None,
    handle: str | None,
    *,
    playlist_map: dict[str, str] | None,
    length_old: int,
    channel_name: str,
) -> VideoCacheResult:
    full_cache_refresh(
        channel_id,
        channel_root,
        cache,
        handle,
        playlist_map=playlist_map,
    )
    return VideoCacheResult(
        videos=[],
        channel_name=channel_name,
        cache_used=False,
        length_curr=0,
        length_old=length_old,
        emergency_refresh=True,
        emergency_message=EMERGENCY_NOTICE,
    )


def touch_cache_validation(cache: dict, channel_root: Path) -> None:
    cache["last_validated_at"] = datetime.now().isoformat(timespec="seconds")
    save_videos_cache(channel_root, cache)


def fetch_complete_live_list(
    channel_id: str,
    first_body: dict | None = None,
) -> tuple[list[dict], str | None]:
    """Walk all browse pages once (used for stale validation or decrease checks)."""
    if first_body is not None:
        videos, channel_name, next_token, _ = fetch_browse_up_to_count(
            channel_id,
            None,
            [],
            first_body,
        )
    else:
        browse_api_header()
        videos, channel_name, next_token, _ = fetch_all_browse_videos(channel_id)
    if next_token is not None:
        return videos, None
    return videos, channel_name


def extend_partial_cache(
    channel_id: str,
    channel_root: Path,
    cache: dict,
    handle: str | None,
    *,
    required: int | None,
    playlist_map: dict[str, str] | None,
) -> VideoCacheResult:
    cached_videos: list[dict] = list(cache["videos"])
    length_curr = len(cached_videos)
    channel_name = cache.get("channel_name") or fetch_channel_name(channel_id)
    length_old = int(cache.get("length_old", length_curr))
    target = extend_target_count(required, length_curr, complete=False)

    print(
        f"Extending partial cache ({length_curr} videos, "
        f"pages 1-{cache.get('pages_fetched') or '?'})...",
        flush=True,
    )
    browse_api_header()
    videos, channel_name, next_token, pages_fetched = fetch_browse_up_to_count(
        channel_id,
        target,
        cached_videos,
        None,
        resume_token=cache.get("continuation_token"),
        resume_after_page=int(cache.get("pages_fetched") or 0),
    )
    cache["videos"] = videos
    cache["channel_name"] = channel_name
    cache["channel_handle"] = handle or cache.get("channel_handle")
    cache["continuation_token"] = next_token
    cache["pages_fetched"] = pages_fetched
    cache["length_curr"] = len(videos)
    if next_token is None:
        cache["length_old"] = len(videos)
        cache["last_validated_at"] = datetime.now().isoformat(timespec="seconds")
        if playlist_map is not None:
            cache["playlist_lengths"] = compute_playlist_lengths(videos, playlist_map)
    save_videos_cache(channel_root, cache)
    return VideoCacheResult(
        videos=videos,
        channel_name=channel_name,
        cache_used=True,
        length_curr=len(videos),
        length_old=int(cache.get("length_old", length_old)),
        emergency_refresh=False,
    )


def ensure_video_cache(
    channel_id: str,
    channel_root: Path,
    handle: str | None,
    *,
    required: int | None,
    playlist_map: dict[str, str] | None = None,
    allow_emergency_refresh: bool = True,
) -> VideoCacheResult:
    cache = load_videos_cache(channel_root)
    if not cache or not cache.get("videos"):
        return full_cache_refresh(
            channel_id,
            channel_root,
            cache,
            handle,
            playlist_map=playlist_map,
        )

    cached_videos: list[dict] = list(cache["videos"])
    channel_name = cache.get("channel_name") or fetch_channel_name(channel_id)
    length_curr = len(cached_videos)
    length_old = int(cache.get("length_old", length_curr))

    if not cache_is_complete(cache):
        return extend_partial_cache(
            channel_id,
            channel_root,
            cache,
            handle,
            required=required,
            playlist_map=playlist_map,
        )

    print("Smart refresh: checking channel video list...", flush=True)
    first_page, live_channel_name, first_body = fetch_first_browse_page(channel_id)
    channel_name = live_channel_name or channel_name
    new_head = new_videos_at_head(first_page, cached_videos)
    n_new = len(new_head)

    if n_new == 0 and not cache_is_stale(cache):
        print("Cache is fresh; no validation needed.", flush=True)
        return VideoCacheResult(
            videos=cached_videos,
            channel_name=channel_name,
            cache_used=True,
            length_curr=length_curr,
            length_old=length_old,
            emergency_refresh=False,
        )

    if n_new == 0 and cache_is_stale(cache):
        print("Cache older than 24h; validating sample videos...", flush=True)
        live_videos, _ = fetch_complete_live_list(channel_id, first_body)
        if len(live_videos) < length_curr:
            print(
                f"Channel video count decreased ({len(live_videos)} < {length_curr}).",
                flush=True,
            )
            if allow_emergency_refresh:
                return emergency_full_refresh(
                    channel_id,
                    channel_root,
                    cache,
                    handle,
                    playlist_map=playlist_map,
                    length_old=length_old,
                    channel_name=channel_name,
                )
            raise SystemExit(
                "Channel video count decreased; run "
                "src\\channels\\refresh_channel_cache.py."
            )
        if not validate_cache_samples(live_videos, cached_videos):
            print("Cache validation failed.", flush=True)
            if allow_emergency_refresh:
                return emergency_full_refresh(
                    channel_id,
                    channel_root,
                    cache,
                    handle,
                    playlist_map=playlist_map,
                    length_old=length_old,
                    channel_name=channel_name,
                )
            raise SystemExit(
                "Cache validation failed; run "
                "src\\channels\\refresh_channel_cache.py."
            )
        touch_cache_validation(cache, channel_root)
        return VideoCacheResult(
            videos=cached_videos,
            channel_name=channel_name,
            cache_used=True,
            length_curr=length_curr,
            length_old=length_old,
            emergency_refresh=False,
        )

    if n_new > 0:
        print(f"Detected {n_new} new video(s) at channel head.", flush=True)
        need = n_new + length_curr
        live_videos, _, _, _ = fetch_browse_up_to_count(
            channel_id,
            need,
            [],
            first_body,
        )
        if len(live_videos) < need:
            print(
                "Could not fetch enough live videos to validate new head; "
                "treating as unchanged cache.",
                flush=True,
            )
            return VideoCacheResult(
                videos=cached_videos,
                channel_name=channel_name,
                cache_used=True,
                length_curr=length_curr,
                length_old=length_old,
                emergency_refresh=False,
            )
        if not validate_cache_samples(live_videos, cached_videos, live_offset=n_new):
            print("Cache validation failed for shifted head.", flush=True)
            if allow_emergency_refresh:
                return emergency_full_refresh(
                    channel_id,
                    channel_root,
                    cache,
                    handle,
                    playlist_map=playlist_map,
                    length_old=length_old,
                    channel_name=channel_name,
                )
            raise SystemExit(
                "Cache validation failed; run "
                "src\\channels\\refresh_channel_cache.py."
            )
        new_videos = live_videos[:n_new]
        videos = merge_new_with_cache(new_videos, cached_videos)
        cache["videos"] = videos
        cache["length_curr"] = len(videos)
        cache["last_validated_at"] = datetime.now().isoformat(timespec="seconds")
        if playlist_map is not None:
            cache["playlist_lengths"] = bump_playlist_lengths_for_new_videos(
                cache.get("playlist_lengths", empty_playlist_lengths()),
                new_videos,
                playlist_map,
            )
        save_videos_cache(channel_root, cache)
        print(f"Prepended {n_new} new video(s) to cache.", flush=True)
        return VideoCacheResult(
            videos=videos,
            channel_name=channel_name,
            cache_used=True,
            length_curr=len(videos),
            length_old=length_old,
            emergency_refresh=False,
        )

    return VideoCacheResult(
        videos=cached_videos,
        channel_name=channel_name,
        cache_used=True,
        length_curr=length_curr,
        length_old=length_old,
        emergency_refresh=False,
    )


def write_emergency_notice(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(message + "\n", encoding="utf-8")
