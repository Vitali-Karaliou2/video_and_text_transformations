#!/usr/bin/env python3
"""Build channel video summaries to TXT and XLSX (pipeline step 1).

Examples:
  python src/get_summary_for_channel.py @Ekaterina_Schulmann
  python src/get_summary_for_channel.py @VladilenMinin --plsonly
  python src/get_summary_for_channel.py @VladilenMinin allpls --from 1 --to 50
  python src/get_summary_for_channel.py @VladilenMinin allpls --new --from 401 --next 200
  python src/get_summary_for_channel.py @VladilenMinin allpls --from "Some unique title prefix"
  python src/get_summary_for_channel.py @VladilenMinin #A --from 1 --to 20
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from summary_helpers import (
    SCOPE_BYPLS,
    SummarySection,
    VideoRecord,
    build_summary_sections,
    normalize_scope,
    scope_mode,
    selected_export_slots,
    summary_output_stem,
    write_plsonly_txt,
    write_plsonly_xlsx,
    write_summary_txt,
    write_summary_xlsx,
)
from channel_browse import fetch_channel_name
from channel_playlists import (
    ensure_playlist_aliases,
    load_video_playlist_map,
    migrate_legacy_browse_cache,
    save_video_playlist_map,
    playlists_cache_path,
    resolve_playlist_selection,
    save_playlists_cache,
    sync_channel_playlists,
)
from project_paths import (
    WORKSPACE_ROOT,
    channel_summaries_dir,
    channels_dir,
    channel_folder_name,
    find_channel_folder,
    normalize_channel_folder_arg,
)
from range_args import (
    apply_resolved_range,
    numeric_required_to,
    resolve_range_args,
)
from video_cache import (
    cache_is_complete,
    commit_length_old,
    ensure_video_cache,
    load_videos_cache,
    save_videos_cache,
    write_emergency_notice,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

WAR_START = date(2022, 2, 24)
STREAM_CUTOFF = date(2020, 1, 1)
DEFAULT_FROM = 1
DEFAULT_TO = 10000
CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
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

from playlist_mapping import build_video_playlist_map


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
    channels_root = args.output_dir or channels_dir(args.workspace)
    resolved_handle = handle
    if not resolved_handle and not explicit:
        resolved_handle = resolve_channel_handle(channel_id, args)

    existing = find_channel_folder(
        channels_root,
        resolved_handle,
        explicit=explicit,
    )
    if existing:
        return existing.name

    if explicit:
        return normalize_channel_folder_arg(explicit)
    if resolved_handle:
        return channel_folder_name(resolved_handle)
    slug = re.sub(r"\W+", "_", channel_name.strip().lower()).strip("_")
    return f"_{slug[:60]}" if slug else f"_{channel_id}"


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
    if record.playlist:
        display_title = f"{record.channel_name} | {record.playlist} : {record.title}"
    else:
        display_title = build_display_title(record.title, record.channel_name)
    return (
        f"{record.url} ({display_title} ) {record.date_text} {record.duration_bracket}"
    )


def console_channel_label(
    channel_id: str,
    handle: str | None,
    channel_name: str,
) -> str:
    """Prefer @handle in console output; YouTube display title may be non-ASCII."""
    if handle:
        return f"{handle} ({channel_id})"
    return f"{channel_name} ({channel_id})"


def parse_excel_date(value: str) -> date | None:
    if value == "????-??-??":
        return None
    if len(value) == 7:
        return date(int(value[:4]), int(value[5:7]), 1)
    return date.fromisoformat(value)


def resolve_playlist_map(
    channel_root: Path,
    channel_id: str,
    args: argparse.Namespace,
    force_refresh: bool,
) -> dict[str, str]:
    if not force_refresh:
        cached_map = load_video_playlist_map(channel_root)
        if cached_map:
            print(
                f"Using cached video playlist map ({len(cached_map)} entries).",
                flush=True,
            )
            return cached_map
    print("Fetching playlists via yt-dlp...", flush=True)
    try:
        mapping = build_video_playlist_map(
            channel_id, lambda *a, **k: yt_dlp_run(args, *a, **k)
        )
    except RuntimeError as exc:
        raise SystemExit(
            f"Playlist lookup failed (playlist column is required):\n{exc}"
        ) from exc
    save_video_playlist_map(channel_root, mapping)
    return mapping


def setup_channel(
    args: argparse.Namespace,
) -> tuple[str, str | None, str, Path, dict | None]:
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

    videos_cache = load_videos_cache(channel_root)
    if not handle and videos_cache and videos_cache.get("channel_handle"):
        handle = normalize_handle(videos_cache["channel_handle"])

    channel_name = (
        videos_cache.get("channel_name")
        if videos_cache and videos_cache.get("channel_name")
        else legacy_name
    )
    return channel_id, handle, channel_name, channel_root, videos_cache


def ensure_playlists_cache(
    channel_root: Path,
    channel_id: str,
    handle: str | None,
    channel_name: str,
    args: argparse.Namespace,
    *,
    force_fetch: bool = False,
) -> dict:
    return sync_channel_playlists(
        channel_root,
        channel_id=channel_id,
        channel_handle=handle,
        channel_name=channel_name,
        run_yt_dlp=lambda *a, **k: yt_dlp_run(args, *a, **k),
        force_fetch=force_fetch,
        create_folders=True,
    )


def run_plsonly(
    *,
    channel_id: str,
    handle: str | None,
    channel_name: str,
    channel_root: Path,
    args: argparse.Namespace,
    now: datetime,
    today: date,
) -> int:
    cache_path = playlists_cache_path(channel_root)
    playlists_cache = ensure_playlists_cache(
        channel_root,
        channel_id,
        handle,
        channel_name,
        args,
        force_fetch=not cache_path.is_file(),
    )
    if ensure_playlist_aliases(playlists_cache):
        save_playlists_cache(playlists_cache_path(channel_root), playlists_cache)

    out_dir = channel_summaries_dir(channel_root, today)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = summary_output_stem(
        out_dir,
        now=now,
        args=args,
        scope=SCOPE_BYPLS,
        plsonly=True,
        single_playlist=None,
        default_from=DEFAULT_FROM,
        default_to=DEFAULT_TO,
    )
    header = f"Playlists for channel {channel_name}  (channel_id = {channel_id} ):"
    rows = [
        (pl.get("alias", ""), channel_name, pl.get("title", ""))
        for pl in playlists_cache.get("playlists", [])
    ]
    txt_path = stem.with_suffix(".txt")
    xlsx_path = stem.with_suffix(".xlsx")
    write_plsonly_txt(txt_path, header, rows)
    write_plsonly_xlsx(xlsx_path, header, rows)

    print(f"Channel: {console_channel_label(channel_id, handle, channel_name)}", flush=True)
    print(f"Playlists: {len(rows)}", flush=True)
    print(f"TXT:   {txt_path}", flush=True)
    print(f"XLSX:  {xlsx_path}", flush=True)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build YouTube channel video summaries to TXT and XLSX."
    )
    parser.add_argument(
        "channel",
        help="YouTube channel id (UC…), @handle, or channel URL",
    )
    parser.add_argument(
        "scope",
        nargs="?",
        default=SCOPE_BYPLS,
        help="bypls (default), allpls, playlist alias (#A), or name prefix",
    )
    parser.add_argument(
        "--plsonly",
        action="store_true",
        help="Export playlist list only (creates _cache/playlists.json if missing)",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help=(
            "Keep baseline numbering (length_old): export new head videos first, "
            "then --from/--to range relative to the frozen baseline list"
        ),
    )
    parser.add_argument(
        "--from",
        dest="from_raw",
        default=str(DEFAULT_FROM),
        help=(
            f"First video number relative to baseline (default: {DEFAULT_FROM}), "
            "or a unique video title / title prefix (>=3 characters, quote if spaced)"
        ),
    )
    parser.add_argument(
        "--to",
        dest="to_raw",
        default=str(DEFAULT_TO),
        help=(
            f"Last video number inclusive relative to baseline (default: {DEFAULT_TO}), "
            "or a unique video title / title prefix (>=3 characters, quote if spaced)"
        ),
    )
    parser.add_argument(
        "--next",
        dest="next_count",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Export the next N videos starting from --from (alternative to --to; "
            "if both are given, the later flag on the command line wins)"
        ),
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
        help="Pass through to yt-dlp for playlist lookup (chrome, edge, firefox)",
    )
    parser.add_argument(
        "--yt-dlp",
        default="yt-dlp",
        help="yt-dlp executable (default: yt-dlp)",
    )
    args = parser.parse_args(argv)
    args.from_index = DEFAULT_FROM
    args.to_index = DEFAULT_TO
    args.from_explicit = False
    args.to_explicit = False
    args.range_end_source = None
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now()
    today = date.today()

    channel_id, handle, channel_name, channel_root, _videos_cache = setup_channel(args)

    if args.plsonly:
        return run_plsonly(
            channel_id=channel_id,
            handle=handle,
            channel_name=channel_name,
            channel_root=channel_root,
            args=args,
            now=now,
            today=today,
        )

    scope = normalize_scope(args.scope)
    playlists_cache = ensure_playlists_cache(
        channel_root,
        channel_id,
        handle,
        channel_name,
        args,
    )

    playlist_map = resolve_playlist_map(
        channel_root,
        channel_id,
        args,
        load_video_playlist_map(channel_root) is None,
    )

    required = numeric_required_to(
        args,
        default_from=DEFAULT_FROM,
        default_to=DEFAULT_TO,
        argv=argv,
    )
    cache_result = ensure_video_cache(
        channel_id,
        channel_root,
        handle,
        required=required,
        playlist_map=playlist_map,
    )

    out_dir = channel_summaries_dir(channel_root, today)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = summary_output_stem(
        out_dir,
        now=now,
        args=args,
        scope=scope,
        plsonly=False,
        single_playlist=None,
        default_from=DEFAULT_FROM,
        default_to=DEFAULT_TO,
        use_new=args.new,
    )

    if cache_result.emergency_refresh:
        notice_path = stem.with_suffix(".txt")
        message = cache_result.emergency_message or "Emergency full cache refresh."
        write_emergency_notice(notice_path, message)
        print(message, flush=True)
        print(f"Notice: {notice_path}", flush=True)
        return 1

    browse_videos = cache_result.videos
    channel_name = cache_result.channel_name or channel_name
    length_curr = cache_result.length_curr
    length_old = cache_result.length_old

    from_index, to_index, from_explicit, to_explicit, range_error, end_source = (
        resolve_range_args(
            args,
            browse_videos,
            length_curr,
            length_old,
            default_from=DEFAULT_FROM,
            default_to=DEFAULT_TO,
            argv=argv,
        )
    )
    if range_error:
        notice_path = stem.with_suffix(".txt")
        write_emergency_notice(notice_path, range_error)
        print(range_error, flush=True)
        print(f"Notice: {notice_path}", flush=True)
        return 1

    apply_resolved_range(
        args,
        from_index,
        to_index,
        from_explicit=from_explicit,
        to_explicit=to_explicit,
        range_end_source=end_source,
    )

    if to_index > len(browse_videos):
        loaded_cache = load_videos_cache(channel_root)
        if loaded_cache and not cache_is_complete(loaded_cache):
            cache_result = ensure_video_cache(
                channel_id,
                channel_root,
                handle,
                required=to_index,
                playlist_map=playlist_map,
            )
            if cache_result.emergency_refresh:
                notice_path = stem.with_suffix(".txt")
                message = cache_result.emergency_message or "Emergency full cache refresh."
                write_emergency_notice(notice_path, message)
                print(message, flush=True)
                print(f"Notice: {notice_path}", flush=True)
                return 1
            browse_videos = cache_result.videos
            channel_name = cache_result.channel_name or channel_name
            length_curr = cache_result.length_curr
            length_old = cache_result.length_old

    export_slots = selected_export_slots(
        length_curr,
        length_old,
        args,
        DEFAULT_FROM,
        DEFAULT_TO,
        use_new=args.new,
    )
    if not export_slots:
        print("No videos selected for export.", flush=True)
        return 0

    sections, total_channel, excluded = build_summary_sections(
        browse_videos,
        channel_name,
        export_slots,
        playlist_map,
        playlists_cache,
        scope,
        today,
        args,
        DEFAULT_FROM,
        DEFAULT_TO,
        length_old,
        use_new=args.new,
        should_include=should_include,
        resolve_date=resolve_date,
        duration_text_to_seconds=duration_text_to_seconds,
        parse_duration_text=parse_duration_text,
    )
    if not any(section.records for section in sections):
        print("No videos matched the requested scope/filter.", flush=True)
        return 0

    single_playlist = (
        resolve_playlist_selection(scope, playlists_cache)
        if scope_mode(scope) == "single"
        else None
    )
    stem = summary_output_stem(
        out_dir,
        now=now,
        args=args,
        scope=scope,
        plsonly=False,
        single_playlist=single_playlist,
        default_from=DEFAULT_FROM,
        default_to=DEFAULT_TO,
        use_new=args.new,
    )
    txt_path = stem.with_suffix(".txt")
    xlsx_path = stem.with_suffix(".xlsx")

    write_summary_txt(txt_path, sections, format_txt_line=format_txt_line)
    write_summary_xlsx(
        xlsx_path,
        sections,
        parse_excel_date=parse_excel_date,
        duration_bracket_to_seconds=duration_bracket_to_seconds,
        build_display_title=build_display_title,
        split_display_title=split_display_title,
    )

    cache = load_videos_cache(channel_root)
    if cache:
        commit_length_old(cache, use_new=args.new)
        save_videos_cache(channel_root, cache)

    exported = sum(len(section.records) for section in sections)
    print(f"Channel: {console_channel_label(channel_id, handle, channel_name)}", flush=True)
    print(f"Scope: {scope}", flush=True)
    print(f"Summary folder: {out_dir}", flush=True)
    print(f"Baseline length: {length_old}", flush=True)
    print(f"Current length: {length_curr}", flush=True)
    print(f"Selected slots: {len(export_slots)}", flush=True)
    print(f"Exported videos: {exported}", flush=True)
    if excluded:
        print(
            f"Excluded from export: {excluded} "
            f"(pre-2020 streams filtered; {total_channel} in cache).",
            flush=True,
        )
    print(f"TXT:   {txt_path}", flush=True)
    print(f"XLSX:  {xlsx_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
