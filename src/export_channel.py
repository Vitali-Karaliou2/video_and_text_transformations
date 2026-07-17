#!/usr/bin/env python3
"""Export YouTube channel video summaries to TXT and XLSX (pipeline step 1).

Examples:
  python src/export_channel.py @Ekaterina_Schulmann
  python src/export_channel.py UCL1rJ0ROIw9V1qFeIN0ZTZQ --new
  python src/export_channel.py @VladilenMinin Vladilen_Minin --from 1 --to 50
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from openpyxl import Workbook

from playlist_mapping import build_video_playlist_map
from project_paths import WORKSPACE_ROOT, cache_dir, output_dir

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
WAR_START = date(2022, 2, 24)
STREAM_CUTOFF = date(2020, 1, 1)
DEFAULT_FROM = 1
DEFAULT_TO = 10000
CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
CACHE_HEAD_CHECK = 3
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BROWSE_ID_RE = re.compile(r'"browseId"\s*:\s*"(UC[\w-]{22})"')
CHANNEL_URL_ID_RE = re.compile(r"channel/(UC[\w-]{22})")
CANONICAL_HANDLE_RE = re.compile(r'"canonicalBaseUrl"\s*:\s*"(/@[^"]+)"')

DAY_PATTERNS = [
    re.compile(r"(?:День|ДЕНЬ|день)\s*(\d+)", re.IGNORECASE),
    re.compile(r"(\d+)\s*[-–—]?\s*(?:й|Й)\s*(?:день|ДЕНЬ)", re.IGNORECASE),
    re.compile(r"(?:^|[\s|])ДЕНЬ\s+(\d+)", re.IGNORECASE),
    re.compile(r"(?:^|[\s.])(\d{3,5})\.\s", re.IGNORECASE),
]
STREAM_HINTS = re.compile(r"(?:стрим|stream|эфир|live\b)", re.IGNORECASE)
RELATIVE_RE = re.compile(
    r"(?P<num>\d+)\s*(?P<unit>second|minute|hour|day|week|month|year)s?\s+ago",
    re.IGNORECASE,
)


@dataclass
class VideoRecord:
    channel_index: int
    video_id: str
    title: str
    channel_name: str
    url: str
    date_text: str
    duration_bracket: str
    duration_seconds: int | None
    playlist: str


@dataclass
class FetchResult:
    videos: list[dict]
    channel_name: str
    new_video_ids: set[str]
    cache_used: bool



def yt_dlp_run(args: argparse.Namespace, *extra: str) -> subprocess.CompletedProcess[str]:
    cmd = [args.yt_dlp, *extra]
    if args.cookies_from_browser:
        cmd[1:1] = ["--cookies-from-browser", args.cookies_from_browser]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def folder_name_from_handle(handle: str) -> str:
    name = handle.strip()
    if name.startswith("@"):
        name = name[1:]
    name = name.replace(" ", "_")
    name = INVALID_PATH_CHARS.sub("", name)
    name = name.strip(" .")
    return name or "unnamed_channel"


def normalize_handle(value: str) -> str:
    value = value.strip()
    if value.startswith("https://www.youtube.com/"):
        value = value.rstrip("/").split("/")[-1]
    if not value.startswith("@"):
        value = "@" + value
    return value


def fetch_youtube_page(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode("utf-8", "replace")


def extract_browse_id(html: str) -> str | None:
    match = BROWSE_ID_RE.search(html)
    if match:
        return match.group(1)
    match = CHANNEL_URL_ID_RE.search(html)
    return match.group(1) if match else None


def extract_canonical_handle(html: str) -> str | None:
    match = CANONICAL_HANDLE_RE.search(html)
    if not match:
        return None
    return normalize_handle(match.group(1).lstrip("/"))


def resolve_channel_id(channel_arg: str, args: argparse.Namespace) -> tuple[str, str | None]:
    raw = channel_arg.strip()
    handle: str | None = None

    if raw.startswith("@"):
        handle = normalize_handle(raw)
        channel_url = f"https://www.youtube.com/{handle}"
        html = fetch_youtube_page(channel_url)
        channel_id = extract_browse_id(html)
        if not channel_id:
            raise SystemExit(f"Could not resolve channel id for {handle}.")
        return channel_id, handle

    if CHANNEL_ID_RE.fullmatch(raw):
        return raw, None

    if raw.startswith("http"):
        html = fetch_youtube_page(raw)
        channel_id = extract_browse_id(html)
        if not channel_id:
            raise SystemExit(f"Could not resolve channel id from URL: {raw}")
        handle = extract_canonical_handle(html)
        return channel_id, handle

    raise SystemExit(
        "First argument must be a channel id (UC…), @handle, or channel URL."
    )


def resolve_channel_handle(channel_id: str, args: argparse.Namespace) -> str | None:
    html = fetch_youtube_page(f"https://www.youtube.com/channel/{channel_id}")
    return extract_canonical_handle(html)


def resolve_output_folder(
    channel_id: str,
    handle: str | None,
    explicit: str | None,
    channel_name: str,
    args: argparse.Namespace,
) -> str:
    if explicit:
        return explicit
    if handle:
        return folder_name_from_handle(handle)
    resolved = resolve_channel_handle(channel_id, args)
    if resolved:
        return folder_name_from_handle(resolved)
    slug = re.sub(r"\W+", "_", channel_name.strip().lower()).strip("_")
    return slug[:60] or channel_id


def post_innertube(endpoint: str, payload: dict, retries: int = 5) -> dict:
    data = json.dumps(payload).encode("utf-8")
    url = f"https://www.youtube.com/youtubei/v1/{endpoint}?key={API_KEY}"
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="POST",
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {429, 500, 503} and attempt + 1 < retries:
                time.sleep(2**attempt)
                continue
            raise
    raise RuntimeError(f"Failed POST {endpoint}")


def client_context() -> dict:
    return {
        "clientName": "WEB",
        "clientVersion": "2.20240410.01.00",
        "hl": "en",
        "gl": "US",
    }


def find_continuations(obj, tokens: list[str]) -> None:
    if isinstance(obj, dict):
        cmd = obj.get("continuationCommand")
        if isinstance(cmd, dict) and cmd.get("token"):
            tokens.append(cmd["token"])
        for value in obj.values():
            find_continuations(value, tokens)
    elif isinstance(obj, list):
        for item in obj:
            find_continuations(item, tokens)


def extract_channel_name(body: dict, fallback: str = "YouTube Channel") -> str:
    metadata = body.get("metadata", {}).get("channelMetadataRenderer", {})
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return fallback


def extract_lockups(body: dict) -> list[dict]:
    lockups: list[dict] = []

    def walk(obj) -> None:
        if isinstance(obj, dict):
            if "lockupViewModel" in obj:
                lockups.append(obj["lockupViewModel"])
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(body)
    return lockups


def parse_duration_text(text: str | None) -> str:
    if not text:
        return "[?:??]"
    text = text.strip()
    if re.fullmatch(r"\d+:\d{2}:\d{2}", text):
        hours, minutes, seconds = text.split(":")
        return f"[{int(hours)}:{int(minutes):02d}:{int(seconds):02d}]"
    if re.fullmatch(r"\d+:\d{2}", text):
        minutes, seconds = text.split(":")
        return f"[{int(minutes)}:{int(seconds):02d}]"
    return f"[{text}]"


def duration_text_to_seconds(text: str | None) -> int | None:
    if not text:
        return None
    text = text.strip()
    if re.fullmatch(r"\d+:\d{2}:\d{2}", text):
        hours, minutes, seconds = map(int, text.split(":"))
        return hours * 3600 + minutes * 60 + seconds
    if re.fullmatch(r"\d+:\d{2}", text):
        minutes, seconds = map(int, text.split(":"))
        return minutes * 60 + seconds
    return None


def duration_bracket_to_seconds(duration: str) -> int | None:
    match = re.fullmatch(r"\[(\d+):(\d{2})(?::(\d{2}))?\]", duration)
    if not match:
        return None
    if match.group(3) is not None:
        return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
    return int(match.group(1)) * 60 + int(match.group(2))


def war_day_from_title(title: str) -> date | None:
    for pattern in DAY_PATTERNS:
        match = pattern.search(title)
        if not match:
            continue
        day_num = int(match.group(1))
        if 1 <= day_num <= 5000:
            return WAR_START + timedelta(days=day_num - 1)
    return None


def parse_relative_date(text: str | None, today: date) -> str:
    if not text:
        return "????-??-??"
    lowered = text.strip().lower()
    if lowered in {"streamed live", "premiere"}:
        return "????-??-??"
    match = RELATIVE_RE.search(lowered)
    if not match:
        return "????-??-??"
    num = int(match.group("num"))
    unit = match.group("unit").lower()
    if unit in {"month", "year"}:
        if unit == "month":
            month_index = today.year * 12 + today.month - num
        else:
            month_index = today.year * 12 + today.month - num * 12
        year, month = divmod(month_index - 1, 12)
        month += 1
        return f"{year:04d}-{month:02d}"
    delta_map = {
        "second": timedelta(seconds=num),
        "minute": timedelta(minutes=num),
        "hour": timedelta(hours=num),
        "day": timedelta(days=num),
        "week": timedelta(weeks=num),
    }
    delta = delta_map.get(unit)
    if not delta:
        return "????-??-??"
    return (today - delta).isoformat()


def is_before_2020(resolved_date: str) -> bool:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved_date):
        return date.fromisoformat(resolved_date) < STREAM_CUTOFF
    if re.fullmatch(r"\d{4}-\d{2}", resolved_date):
        return date(int(resolved_date[:4]), int(resolved_date[5:7]), 1) < STREAM_CUTOFF
    return False


def is_stream(title: str, duration_seconds: int | None) -> bool:
    if STREAM_HINTS.search(title):
        return True
    if duration_seconds and duration_seconds >= 3600 and re.search(
        r"в гостях|Плющев и Наки|Плющева и Наки|Плющева с Наки",
        title,
        re.IGNORECASE,
    ):
        return True
    if duration_seconds and duration_seconds >= 7200 and war_day_from_title(title):
        return True
    return False


def resolve_date(title: str, relative_published: str | None, today: date) -> str:
    war_day = war_day_from_title(title)
    if war_day:
        return war_day.isoformat()
    return parse_relative_date(relative_published, today)


def should_include(title: str, resolved_date: str, duration_seconds: int | None) -> bool:
    return not (is_before_2020(resolved_date) and is_stream(title, duration_seconds))


def parse_lockup(lockup: dict) -> dict | None:
    video_id = lockup.get("contentId")
    if not video_id:
        return None
    meta = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = (meta.get("title", {}) or {}).get("content", "").strip()
    if not title:
        return None

    relative_published = None
    rows = (
        meta.get("metadata", {})
        .get("contentMetadataViewModel", {})
        .get("metadataRows", [])
        or []
    )
    for row in rows:
        for part in row.get("metadataParts", []) or []:
            text_obj = part.get("text", {}) or {}
            text = text_obj.get("content") or text_obj.get("simpleText")
            if text and "view" not in text.lower() and "watching" not in text.lower():
                relative_published = text
                break
        if relative_published:
            break

    duration_text = None
    overlays = (
        lockup.get("contentImage", {})
        .get("thumbnailViewModel", {})
        .get("overlays", [])
        or []
    )
    for overlay in overlays:
        badges = overlay.get("thumbnailBottomOverlayViewModel", {}).get("badges", []) or []
        for badge in badges:
            text = badge.get("thumbnailBadgeViewModel", {}).get("text")
            if text and re.search(r"\d", text):
                duration_text = text
                break
        if duration_text:
            break

    return {
        "id": video_id,
        "title": title,
        "duration_text": duration_text,
        "relative_published": relative_published,
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }


def cache_file_path(channel_id: str, workspace: Path) -> Path:
    return cache_dir(workspace) / f"{channel_id}.json"


def read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_cache(
    path: Path,
    *,
    channel_id: str,
    channel_name: str,
    channel_handle: str | None,
    videos: list[dict],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "channel_handle": channel_handle,
        "count": len(videos),
        "last_fetched_at": datetime.now().isoformat(timespec="seconds"),
        "videos": videos,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def fetch_channel_name(channel_id: str) -> str:
    payload = {
        "context": {"client": client_context()},
        "browseId": channel_id,
        "params": "EgZ2aWRlb3PyBgQKAjoA",
    }
    body = post_innertube("browse", payload)
    return extract_channel_name(body)


def fetch_browse_page(channel_id: str, continuation: str | None = None) -> dict:
    if continuation:
        payload = {"context": {"client": client_context()}, "continuation": continuation}
    else:
        payload = {
            "context": {"client": client_context()},
            "browseId": channel_id,
            "params": "EgZ2aWRlb3PyBgQKAjoA",
        }
    return post_innertube("browse", payload)


def ingest_response(response: dict, videos: dict[str, dict]) -> int:
    added = 0
    for lockup in extract_lockups(response):
        parsed = parse_lockup(lockup)
        if parsed and parsed["id"] not in videos:
            videos[parsed["id"]] = parsed
            added += 1
    return added


def fetch_all_browse_videos(channel_id: str) -> tuple[list[dict], str]:
    print("Fetching channel videos via YouTube browse API...", flush=True)
    body = fetch_browse_page(channel_id)
    channel_name = extract_channel_name(body)
    videos: dict[str, dict] = {}
    ingest_response(body, videos)

    tokens: list[str] = []
    find_continuations(body, tokens)
    token = tokens[0] if tokens else None
    page = 1

    while token:
        page += 1
        response = fetch_browse_page(channel_id, token)
        added = ingest_response(response, videos)
        print(f"Page {page}: total {len(videos)} (+{added})", flush=True)
        tokens = []
        find_continuations(response, tokens)
        token = tokens[0] if tokens else None
        time.sleep(0.4)

    return list(videos.values()), channel_name


def fetch_first_page_videos(channel_id: str) -> tuple[list[dict], str]:
    body = fetch_browse_page(channel_id)
    channel_name = extract_channel_name(body)
    videos: list[dict] = []
    for lockup in extract_lockups(body):
        parsed = parse_lockup(lockup)
        if parsed:
            videos.append(parsed)
    return videos, channel_name


def fetch_until_known(channel_id: str, known_ids: set[str]) -> tuple[list[dict], str]:
    """Fetch pages until the first previously cached video appears."""
    print("Fetching recent channel videos...", flush=True)
    body = fetch_browse_page(channel_id)
    channel_name = extract_channel_name(body)
    collected: list[dict] = []
    seen: set[str] = set()

    def add_from_response(response: dict) -> bool:
        hit_known = False
        for lockup in extract_lockups(response):
            parsed = parse_lockup(lockup)
            if not parsed:
                continue
            if parsed["id"] in known_ids:
                hit_known = True
                break
            if parsed["id"] not in seen:
                seen.add(parsed["id"])
                collected.append(parsed)
        return hit_known

    if add_from_response(body):
        return collected, channel_name

    tokens: list[str] = []
    find_continuations(body, tokens)
    token = tokens[0] if tokens else None
    page = 1
    while token:
        page += 1
        response = fetch_browse_page(channel_id, token)
        if add_from_response(response):
            break
        tokens = []
        find_continuations(response, tokens)
        token = tokens[0] if tokens else None
        time.sleep(0.4)

    return collected, channel_name


def new_videos_at_head(fresh: list[dict], cached: list[dict]) -> list[dict]:
    cached_ids = {v["id"] for v in cached}
    new: list[dict] = []
    for video in fresh:
        if video["id"] not in cached_ids:
            new.append(video)
        else:
            break
    return new


def cache_head_matches(fresh: list[dict], cached: list[dict], n: int = CACHE_HEAD_CHECK) -> bool:
    limit = min(n, len(fresh), len(cached))
    if limit == 0:
        return False
    for i in range(limit):
        if fresh[i]["id"] != cached[i]["id"]:
            return False
        if fresh[i].get("title") != cached[i].get("title"):
            return False
    return True


def merge_new_with_cache(new_videos: list[dict], cached_videos: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {v["id"]: v for v in cached_videos}
    for video in reversed(new_videos):
        merged[video["id"]] = video
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


def load_browse_videos(
    channel_id: str,
    args: argparse.Namespace,
    cache: dict | None,
    handle: str | None,
) -> FetchResult:
    cache_path = cache_file_path(channel_id, args.workspace)
    has_cache = bool(cache and cache.get("videos"))

    if not has_cache:
        if args.refresh or args.new:
            print("No cache yet; --refresh/--new ignored.", flush=True)
        videos, channel_name = fetch_all_browse_videos(channel_id)
        write_cache(
            cache_path,
            channel_id=channel_id,
            channel_name=channel_name,
            channel_handle=handle or (cache.get("channel_handle") if cache else None),
            videos=videos,
        )
        return FetchResult(videos, channel_name, set(), False)

    cached_videos: list[dict] = cache["videos"]
    channel_name = cache.get("channel_name") or fetch_channel_name(channel_id)

    if args.refresh:
        videos, channel_name = fetch_all_browse_videos(channel_id)
        write_cache(
            cache_path,
            channel_id=channel_id,
            channel_name=channel_name,
            channel_handle=handle or cache.get("channel_handle"),
            videos=videos,
        )
        return FetchResult(videos, channel_name, set(), False)

    known_ids = {v["id"] for v in cached_videos}

    if args.new:
        fresh_new, channel_name = fetch_until_known(channel_id, known_ids)
        new_ids = {v["id"] for v in fresh_new}
        if fresh_new:
            merged = merge_new_with_cache(fresh_new, cached_videos)
            write_cache(
                cache_path,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_handle=handle or cache.get("channel_handle"),
                videos=merged,
            )
            return FetchResult(merged, channel_name, new_ids, True)
        print("No new videos found.", flush=True)
        return FetchResult(cached_videos, channel_name, set(), True)

    # Smart refresh: verify cached head against live first page, then reuse cache
    first_page, channel_name = fetch_first_page_videos(channel_id)
    if cache_head_matches(first_page, cached_videos):
        fresh_new, _ = fetch_until_known(channel_id, known_ids)
        new_list = fresh_new
        new_ids = {v["id"] for v in new_list}
        if new_list:
            merged = merge_new_with_cache(new_list, cached_videos)
            write_cache(
                cache_path,
                channel_id=channel_id,
                channel_name=channel_name,
                channel_handle=handle or cache.get("channel_handle"),
                videos=merged,
            )
            print(
                f"Cache head OK; appended {len(fresh_new)} new video(s), "
                f"reused {len(cached_videos)} cached entries.",
                flush=True,
            )
            return FetchResult(merged, channel_name, new_ids, True)
        print(f"Using cached browse data ({len(cached_videos)} videos)", flush=True)
        return FetchResult(cached_videos, channel_name, set(), True)

    print("Cache head mismatch; performing full refresh.", flush=True)
    videos, channel_name = fetch_all_browse_videos(channel_id)
    write_cache(
        cache_path,
        channel_id=channel_id,
        channel_name=channel_name,
        channel_handle=handle or cache.get("channel_handle"),
        videos=videos,
    )
    return FetchResult(videos, channel_name, set(), False)


def range_explicit(args: argparse.Namespace) -> bool:
    return args.from_index != DEFAULT_FROM or args.to_index != DEFAULT_TO


def selected_channel_indices(
    total: int,
    args: argparse.Namespace,
    new_video_ids: set[str],
    browse_videos: list[dict],
) -> list[int]:
    if args.new and not range_explicit(args):
        indices = [
            i
            for i, video in enumerate(browse_videos, start=1)
            if video["id"] in new_video_ids
        ]
        return indices

    indices: set[int] = set()
    if args.new:
        for i, video in enumerate(browse_videos, start=1):
            if video["id"] in new_video_ids:
                indices.add(i)

    start = max(1, args.from_index)
    end = min(args.to_index, total)
    if start <= end:
        indices.update(range(start, end + 1))
    return sorted(indices)


def build_display_title(title: str, channel_name: str) -> str:
    if re.search(r"\|\s*" + re.escape(channel_name) + r"\s*$", title):
        return title
    return f"{title} | {channel_name}"


def split_display_title(display_title: str, channel_name: str) -> tuple[str, str]:
    match = re.search(r"\s+\|\s+" + re.escape(channel_name) + r"\s*$", display_title)
    if match:
        return display_title[: match.start()].strip(), channel_name
    return display_title.strip(), channel_name


def format_txt_line(record: VideoRecord) -> str:
    display_title = build_display_title(record.title, record.channel_name)
    return (
        f"{record.url} ({display_title} ) {record.date_text} {record.duration_bracket}"
    )


def build_records(
    browse_videos: list[dict],
    channel_name: str,
    selected_indices: list[int],
    playlist_map: dict[str, str],
    today: date,
) -> tuple[list[VideoRecord], int]:
    total_channel = len(browse_videos)
    records: list[VideoRecord] = []

    for index in selected_indices:
        if index < 1 or index > total_channel:
            continue
        entry = browse_videos[index - 1]
        duration_seconds = duration_text_to_seconds(entry.get("duration_text"))
        date_text = resolve_date(entry["title"], entry.get("relative_published"), today)
        if not should_include(entry["title"], date_text, duration_seconds):
            continue
        records.append(
            VideoRecord(
                channel_index=index,
                video_id=entry["id"],
                title=entry["title"].strip(),
                channel_name=channel_name,
                url=entry["url"],
                date_text=date_text,
                duration_bracket=parse_duration_text(entry.get("duration_text")),
                duration_seconds=duration_seconds,
                playlist=playlist_map.get(entry["id"], ""),
            )
        )

    return records, total_channel


def summary_line(indices: list[int], total_channel: int) -> str:
    if not indices:
        return f"Videos none selected from total {total_channel} videos."
    if len(indices) == 1:
        return f"Video {indices[0]} from total {total_channel} videos."
    return (
        f"Videos {indices[0]} to {indices[-1]} "
        f"({len(indices)} selected) from total {total_channel} videos."
    )


def output_stem(base_path: Path, indices: list[int], total_channel: int) -> Path:
    if len(indices) == 1:
        return base_path.parent / f"{base_path.name}_{indices[0]}"
    if len(indices) >= 2 and not (
        indices[0] == 1 and indices[-1] >= total_channel and len(indices) == total_channel
    ):
        return base_path.parent / f"{base_path.name}_{indices[0]}_{indices[-1]}"
    return base_path


def parse_excel_date(value: str) -> date | None:
    if value == "????-??-??":
        return None
    if len(value) == 7:
        return date(int(value[:4]), int(value[5:7]), 1)
    return date.fromisoformat(value)


def write_txt(path: Path, summary: str, records: list[VideoRecord]) -> None:
    lines = [summary, * (format_txt_line(record) for record in records)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_xlsx(path: Path, summary: str, records: list[VideoRecord]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Videos"
    ws.cell(row=1, column=1, value=summary)

    row = 2
    for record in records:
        parsed_date = parse_excel_date(record.date_text)
        ws.cell(row=row, column=1, value=record.channel_index)
        ws.cell(row=row, column=2, value=record.channel_name)
        ws.cell(row=row, column=3, value=record.playlist)
        ws.cell(row=row, column=4, value=record.url)
        date_cell = ws.cell(row=row, column=5, value=parsed_date)
        if parsed_date is not None:
            date_cell.number_format = (
                "yyyy-mm-dd" if len(record.date_text) == 10 else "yyyy-mm"
            )
        duration_seconds = record.duration_seconds or duration_bracket_to_seconds(
            record.duration_bracket
        )
        if duration_seconds is not None:
            duration_cell = ws.cell(row=row, column=6, value=duration_seconds / 86400)
            duration_cell.number_format = "[h]:mm:ss"
        display_title = build_display_title(record.title, record.channel_name)
        title_only, _ = split_display_title(display_title, record.channel_name)
        ws.cell(row=row, column=7, value=title_only)
        row += 1

    wb.save(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export YouTube channel video summaries to TXT and XLSX."
    )
    parser.add_argument(
        "channel",
        help="YouTube channel id (UC…), @handle, or channel URL",
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Optional output folder name under output/ (default: derived from @handle)",
    )
    parser.add_argument(
        "--from",
        dest="from_index",
        type=int,
        default=DEFAULT_FROM,
        help=f"First video number on channel (default: {DEFAULT_FROM})",
    )
    parser.add_argument(
        "--to",
        dest="to_index",
        type=int,
        default=DEFAULT_TO,
        help=f"Last video number inclusive (default: {DEFAULT_TO})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cache and reload all channel data from YouTube",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="Include only videos published since the previous export "
        "(unless --from/--to are also set)",
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
        help="Parent directory for channel folders (default: <workspace>/output)",
    )
    parser.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Pass through to yt-dlp for playlist lookup (chrome, edge, firefox)",
    )
    parser.add_argument(
        "--yt-dlp",
        default="yt-dlp",
        help="yt-dlp executable (default: yt-dlp)",
    )
    parser.add_argument(
        "--skip-playlists",
        action="store_true",
        help="Do not fetch playlist mapping (playlist column will be empty)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    today = date.today()

    channel_id, handle = resolve_channel_id(args.channel, args)
    cache = read_cache(cache_file_path(channel_id, args.workspace))

    fetch = load_browse_videos(channel_id, args, cache, handle)
    browse_videos = fetch.videos
    channel_name = fetch.channel_name

    folder_name = resolve_output_folder(
        channel_id, handle, args.output, channel_name, args
    )
    out_base = (args.output_dir or output_dir(args.workspace)) / folder_name
    out_base.mkdir(parents=True, exist_ok=True)

    selected = selected_channel_indices(
        len(browse_videos), args, fetch.new_video_ids, browse_videos
    )
    if not selected:
        print("No videos selected for export.", flush=True)
        return 0

    playlist_map: dict[str, str] = {}
    if not args.skip_playlists:
        print("Fetching playlists for playlist column...", flush=True)
        try:
            playlist_map = build_video_playlist_map(
                channel_id, lambda *a, **k: yt_dlp_run(args, *a, **k)
            )
        except RuntimeError as exc:
            print(f"WARN: playlist lookup failed: {exc}", flush=True)

    records, total_channel = build_records(
        browse_videos, channel_name, selected, playlist_map, today
    )
    summary = summary_line(selected, total_channel)
    stem = output_stem(out_base / folder_name, selected, total_channel)
    txt_path = stem.with_suffix(".txt")
    xlsx_path = stem.with_suffix(".xlsx")

    write_txt(txt_path, summary, records)
    write_xlsx(xlsx_path, summary, records)

    print(f"Channel: {channel_name} ({channel_id})", flush=True)
    if handle:
        print(f"Handle: {handle}", flush=True)
    print(f"Output folder: {out_base}", flush=True)
    print(f"Selected indices: {len(selected)}", flush=True)
    print(f"Exported videos: {len(records)}", flush=True)
    print(f"TXT:   {txt_path}", flush=True)
    print(f"XLSX:  {xlsx_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
