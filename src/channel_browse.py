"""YouTube InnerTube browse API helpers for channel video lists."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
PAGE_SIZE = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


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
        badges = overlay.get("thumbnailBadgeViewModel", {}).get("badges", []) or []
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


def ingest_lockups_from_response(
    response: dict,
    videos: list[dict],
    seen_ids: set[str],
) -> tuple[int, int]:
    lockups = extract_lockups(response)
    added = 0
    for lockup in lockups:
        parsed = parse_lockup(lockup)
        if parsed and parsed["id"] not in seen_ids:
            seen_ids.add(parsed["id"])
            videos.append(parsed)
            added += 1
    return added, len(lockups)


def log_page_ingest(
    page: int,
    total: int,
    added: int,
    items: int,
    *,
    is_last: bool = False,
) -> None:
    if is_last:
        suffix = ", last page"
    elif items and added == 0 and total == 0:
        suffix = f", 0 parsed from {items} lockups"
    elif added == 0:
        suffix = ", all already in cache"
    elif added < items:
        suffix = f", {items - added} skipped"
    else:
        suffix = ""
    print(f"Page {page}: total {total} (+{added}{suffix})", flush=True)


def browse_api_header() -> None:
    print("Fetching channel videos via YouTube browse API...", flush=True)


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


def first_continuation(body: dict) -> str | None:
    tokens: list[str] = []
    find_continuations(body, tokens)
    return tokens[0] if tokens else None


def fetch_all_browse_videos(channel_id: str) -> tuple[list[dict], str, str | None, int]:
    browse_api_header()
    body = fetch_browse_page(channel_id)
    channel_name = extract_channel_name(body)
    videos: list[dict] = []
    seen_ids: set[str] = set()
    added, items = ingest_lockups_from_response(body, videos, seen_ids)
    log_page_ingest(1, len(videos), added, items)

    token = first_continuation(body)
    page = 1

    while token:
        page += 1
        response = fetch_browse_page(channel_id, token)
        added, items = ingest_lockups_from_response(response, videos, seen_ids)
        token = first_continuation(response)
        if items == 0:
            log_page_ingest(page, len(videos), added, items, is_last=True)
            break
        log_page_ingest(page, len(videos), added, items, is_last=not token)
        time.sleep(0.4)

    print(
        f"Channel browse complete: {len(videos)} videos in {page} page(s).",
        flush=True,
    )
    return videos, channel_name, None, page


def fetch_first_browse_page(
    channel_id: str,
    *,
    known_ids: set[str] | None = None,
    announce: bool = True,
) -> tuple[list[dict], str, dict]:
    if announce:
        browse_api_header()
    body = fetch_browse_page(channel_id)
    channel_name = extract_channel_name(body)
    lockups = extract_lockups(body)
    videos: list[dict] = []
    added = 0
    for lockup in lockups:
        parsed = parse_lockup(lockup)
        if not parsed:
            continue
        videos.append(parsed)
        if known_ids is None or parsed["id"] not in known_ids:
            added += 1
    log_page_ingest(1, len(videos), added, len(lockups))
    return videos, channel_name, body


def fetch_browse_up_to_count(
    channel_id: str,
    min_count: int | None,
    existing: list[dict],
    first_body: dict | None = None,
    *,
    resume_token: str | None = None,
    resume_after_page: int = 0,
) -> tuple[list[dict], str, str | None, int]:
    videos = list(existing)
    seen_ids = {v["id"] for v in videos}

    if min_count is not None and len(videos) >= min_count:
        if first_body is not None and resume_after_page == 0 and resume_token is None:
            return (
                videos,
                extract_channel_name(first_body),
                first_continuation(first_body) if len(videos) >= PAGE_SIZE else None,
                max(1, resume_after_page or 1),
            )
        channel_name = (
            extract_channel_name(first_body) if first_body else fetch_channel_name(channel_id)
        )
        return videos, channel_name, resume_token, resume_after_page

    page = resume_after_page
    pages_fetched = resume_after_page
    token: str | None = None
    channel_name = ""

    if resume_token:
        channel_name = (
            extract_channel_name(first_body) if first_body else fetch_channel_name(channel_id)
        )
        print(
            f"Resuming from page {page + 1} ({len(videos)} videos already in cache, "
            f"no re-fetch of page{'s' if page > 1 else ''} "
            f"{'1-' + str(page) if page > 1 else '1'})...",
            flush=True,
        )
        token = resume_token
    else:
        if first_body is not None:
            body = first_body
        else:
            browse_api_header()
            body = fetch_browse_page(channel_id)
        channel_name = extract_channel_name(body)
        page = 1
        pages_fetched = 1

        if len(videos) < PAGE_SIZE:
            added, items = ingest_lockups_from_response(body, videos, seen_ids)
            if page == 1 and not resume_token:
                log_page_ingest(1, len(videos), added, items)
            if added == 0 and items > 0 and len(videos) == 0:
                raise SystemExit(
                    f"Could not parse any videos from page 1 ({items} lockups on page)."
                )

        if min_count is not None and len(videos) >= min_count:
            return videos, channel_name, first_continuation(body), pages_fetched

        token = first_continuation(body)
        if token and len(videos) >= PAGE_SIZE and min_count is not None:
            print(
                f"Walking from page 2 ({len(videos)} videos in cache, "
                f"no saved continuation token)...",
                flush=True,
            )

    while token and (min_count is None or len(videos) < min_count):
        page += 1
        response = fetch_browse_page(channel_id, token)
        added, items = ingest_lockups_from_response(response, videos, seen_ids)
        pages_fetched = page
        token = first_continuation(response)
        if items == 0:
            log_page_ingest(page, len(videos), added, items, is_last=True)
            print("Empty browse page; channel list complete.", flush=True)
            token = None
            break
        log_page_ingest(page, len(videos), added, items, is_last=not token)
        if min_count is not None and len(videos) >= min_count:
            break
        if token:
            time.sleep(0.4)

    if min_count is None and pages_fetched:
        print(
            f"Channel browse complete: {len(videos)} videos in {pages_fetched} page(s).",
            flush=True,
        )

    next_token = token if token else None
    return videos, channel_name, next_token, pages_fetched


def pages_for_index(index: int) -> int:
    return max(1, (index - 1) // PAGE_SIZE + 1)


def new_videos_at_head(fresh: list[dict], cached: list[dict]) -> list[dict]:
    cached_ids = {v["id"] for v in cached}
    new: list[dict] = []
    for video in fresh:
        if video["id"] not in cached_ids:
            new.append(video)
        else:
            break
    return new
