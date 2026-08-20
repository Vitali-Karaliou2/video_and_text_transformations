#!/usr/bin/env python3
"""Find a YouTube channel by a free-text description and start its pipeline.

The description (e.g. "Политолог Аббас Галлямов YouTube") is searched on
YouTube with the channel filter; up to three best-matching channels are
shown in decreasing relevance order. For every candidate the script prints:

- channel name, @handle, subscriber count and description;
- the total number of uploaded videos;
- every playlist with its video count; playlist titles that are neither
  Russian nor a transliteration of Russian also get a Russian translation
  (one small OpenAI call per candidate; skipped when no API key is set);
- whether the channel already exists under _channels/ and whether it
  already has summaries.

Then, for each candidate in turn, the script asks for a confirmation
(y = build the summary, n = next candidate, q = quit). On "y" it asks how
to group the summary (by playlists / flat), runs get_summary_for_channel
(which creates _cache, _playlists and _summaries and fills them), makes
sure the channel folder has the standard subfolders (_cache, _playlists,
_run_scripts, _summaries) and generates the automation bat files in the
channel's _run_scripts:

- transcribe_and_edit_next.bat      - transcribe + edit the next
  untranscribed (or transcribed-but-unedited) video from the flat
  channel-wide video list;
- transcribe_and_edit_next_by_substr.bat - the same over the whole channel,
  but only for the videos whose title contains the substring set in the
  sibling .settings.txt (TITLE_SUBSTR); every match is confirmed in turn;
- transcribe_and_edit_next_bypl.bat - the same for one playlist; the
  playlist folder is set in the sibling .settings.txt (PLAYLIST; default:
  the real playlist with the most videos);
- refresh_summary.bat - after a long pause: force-refresh _cache/videos.json
  from YouTube, then rebuild the channel summary (by playlists).

The transcription bats use transcribe_videos.py --from-youtube --edit
--annotate --orig-only, so the edited documents are produced in the
original language only, with 200-250 word annotations (Russian original
-> Russian annotation; other originals -> English + Russian annotations).

Usage:
  python src/channels/find_youtube_channel_by_descr.py "Политолог Аббас Галлямов YouTube"
  python src/channels/find_youtube_channel_by_descr.py --settings <file>

Automation: _run_scripts/add_youtube_channel_by_descr.bat (project root). The
description to search for and the folder to create the channel in are edited
in add_youtube_channel_by_descr.settings.txt next to that bat, which the bat
passes here as --settings; see shared/settings_file.py for why they are not
kept in the bat itself. The same pattern is used for the channel automation
bats it generates: CHANNEL, TITLE_SUBSTR, PLAYLIST and HANDLE live in a
sibling .settings.txt next to each bat.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Collection
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from channels.channel_browse import (
    client_context,
    extract_lockups,
    find_continuations,
    post_innertube,
)
from shared.project_paths import (
    WORKSPACE_ROOT,
    channel_folder_name,
    channel_relative_ref,
    channels_dir,
    find_channel_folder,
)
from shared.settings_file import read_settings, settings_beside, write_settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MAX_CANDIDATES = 3
# What the settings file of the root bat may hold (see shared/settings_file.py
# for why the bat keeps its editable values in a file of their own).
SETTINGS_KEYS = ("DESCR", "CHANNEL_PATH")
# YouTube search filter "Type: Channel" (sp parameter, already URL-encoded).
CHANNEL_FILTER_SP = "EgIQAg%253D%253D"
PLAYLISTS_TAB_PARAMS = "EglwbGF5bGlzdHPyBgQKAkIA"

TRANSLATE_SYSTEM_PROMPT = """\
You are given YouTube channel metadata: the channel title, its description
and the list of playlist titles.

1. Guess the dominant content language of the channel (the language its
   videos are most likely spoken in) as a two-letter lower-case code.
2. Translate every playlist title into Russian, in the given order.
   Return null ONLY for titles that are already in Russian or are a
   transliteration of Russian; every other title (including titles with
   emojis, hashtags or brand names) must get a Russian translation.

Reply with JSON only:
{"language": "xx", "translations": ["...", null, ...]}
("translations" has exactly one item per playlist title, same order)."""


def run_yt_dlp_json(args_list: list[str]) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp", *args_list],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"yt-dlp failed:\n{result.stderr.strip()[-2000:]}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise SystemExit("yt-dlp returned invalid JSON")


def search_channels(description: str, limit: int = MAX_CANDIDATES) -> list[dict]:
    """Top channels for the description, in YouTube relevance order.

    First a channel-filtered search (matches channel names/descriptions);
    when it finds nothing - e.g. the description describes the *content*
    rather than the channel name - fall back to a regular video search and
    take the channels of the top matching videos."""
    url = (
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote(description)
        + f"&sp={CHANNEL_FILTER_SP}"
    )
    data = run_yt_dlp_json(["--flat-playlist", "-J", url])
    candidates: list[dict] = []
    for entry in data.get("entries") or []:
        channel_id = entry.get("channel_id") or entry.get("id") or ""
        if not channel_id.startswith("UC"):
            continue
        candidates.append(
            {
                "id": channel_id,
                "title": str(entry.get("channel") or entry.get("title") or ""),
                "handle": str(entry.get("uploader_id") or ""),
                "url": entry.get("channel_url")
                or f"https://www.youtube.com/channel/{channel_id}",
                "followers": entry.get("channel_follower_count"),
                "description": str(entry.get("description") or ""),
            }
        )
        if len(candidates) >= limit:
            break
    if candidates:
        return candidates

    print(
        "No channel matched the description directly; looking at the "
        "channels of the top matching videos...",
        flush=True,
    )
    data = run_yt_dlp_json(
        ["--flat-playlist", "-J", f"ytsearch{limit * 8}:{description}"]
    )
    seen: set[str] = set()
    for entry in data.get("entries") or []:
        channel_id = str(entry.get("channel_id") or "")
        if not channel_id.startswith("UC") or channel_id in seen:
            continue
        seen.add(channel_id)
        candidates.append(
            {
                "id": channel_id,
                "title": str(entry.get("channel") or entry.get("uploader") or ""),
                "handle": str(entry.get("uploader_id") or ""),
                "url": entry.get("channel_url")
                or f"https://www.youtube.com/channel/{channel_id}",
                "followers": entry.get("channel_follower_count"),
                "description": "",
                "via_video": str(entry.get("title") or ""),
            }
        )
        if len(candidates) >= limit:
            break
    return candidates


def fetch_uploads_count(channel_id: str) -> int | None:
    """Total videos in the channel uploads playlist (UU...), one request."""
    uploads = "UU" + channel_id[2:]
    result = subprocess.run(
        [
            sys.executable, "-m", "yt_dlp",
            "--flat-playlist", "-J", "-I", "1:1",
            f"https://www.youtube.com/playlist?list={uploads}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    try:
        count = json.loads(result.stdout).get("playlist_count")
    except ValueError:
        return None
    return int(count) if isinstance(count, (int, float)) else None


def parse_count_text(text: str) -> int | None:
    match = re.search(r"([\d][\d,.\s\u00a0]*)\s*video", text, re.IGNORECASE)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(1))
    return int(digits) if digits else None


def playlist_lockup_fields(lockup: dict) -> tuple[str | None, str, int | None]:
    """(playlist id, title, video count) from a playlists-tab lockup."""
    playlist_id = lockup.get("contentId")
    meta = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
    title = str((meta.get("title", {}) or {}).get("content") or "").strip()
    count: int | None = None

    def walk(obj) -> None:
        nonlocal count
        if count is not None:
            return
        if isinstance(obj, dict):
            badge = obj.get("thumbnailBadgeViewModel")
            if isinstance(badge, dict) and badge.get("text"):
                parsed = parse_count_text(str(badge["text"]))
                if parsed is not None:
                    count = parsed
                    return
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(lockup.get("contentImage") or {})
    return playlist_id, title, count


def fetch_playlists_with_counts(channel_id: str) -> list[dict]:
    """All channel playlists with video counts (InnerTube playlists tab)."""
    body = post_innertube(
        "browse",
        {
            "context": {"client": client_context()},
            "browseId": channel_id,
            "params": PLAYLISTS_TAB_PARAMS,
        },
    )
    playlists: list[dict] = []
    seen: set[str] = set()

    def ingest(response: dict) -> int:
        added = 0
        for lockup in extract_lockups(response):
            # When a channel has no playlists tab, YouTube falls back to
            # another tab and the lockups are videos - skip those.
            content_type = str(lockup.get("contentType") or "")
            playlist_id, title, count = playlist_lockup_fields(lockup)
            is_playlist = "PLAYLIST" in content_type or bool(
                playlist_id
                and re.match(r"^(PL|UU|FL|OL|RD)", playlist_id)
            )
            if not is_playlist:
                continue
            if not playlist_id or playlist_id in seen or not title:
                continue
            seen.add(playlist_id)
            playlists.append({"id": playlist_id, "title": title, "count": count})
            added += 1
        return added

    added = ingest(body)
    tokens: list[str] = []
    find_continuations(body, tokens)
    token = tokens[0] if tokens else None
    while token and added:
        response = post_innertube(
            "browse",
            {"context": {"client": client_context()}, "continuation": token},
        )
        added = ingest(response)
        tokens = []
        find_continuations(response, tokens)
        token = tokens[0] if tokens else None
        time.sleep(0.3)
    return playlists


def looks_russian_or_translit(text: str) -> bool:
    """Cheap local check to skip the API when every title is Russian."""
    return bool(re.search(r"[а-яё]", text, re.IGNORECASE))


def translate_playlists(
    api_key: str | None,
    candidate: dict,
    playlists: list[dict],
) -> tuple[str | None, list[str | None]]:
    """(guessed content language, Russian translation per playlist title)."""
    titles = [pl["title"] for pl in playlists]
    if api_key is None:
        return None, [None] * len(titles)
    from slides.text_from_slides import chat_json

    payload = {
        "channel_title": candidate["title"],
        "channel_description": candidate["description"][:1000],
        "playlist_titles": titles,
    }
    try:
        result = chat_json(
            api_key,
            [
                {"role": "system", "content": TRANSLATE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            {},
        )
    except Exception as exc:  # noqa: BLE001 - display info is best-effort
        print(f"  WARNING: translation call failed: {exc}", flush=True)
        return None, [None] * len(titles)
    language = str(result.get("language") or "").strip().lower() or None
    raw = result.get("translations")
    translations: list[str | None] = []
    for index in range(len(titles)):
        value = raw[index] if isinstance(raw, list) and index < len(raw) else None
        text = str(value).strip() if value else ""
        translations.append(text or None)
    return language, translations


def locate_channel_root(channel_id: str, handle: str | None) -> Path | None:
    """Existing folder under _channels/ for this channel, if any."""
    root = channels_dir(WORKSPACE_ROOT)
    return find_channel_folder(root, handle, channel_id=channel_id)


def summaries_present(channel_root: Path) -> bool:
    summaries = channel_root / "_summaries"
    if not summaries.is_dir():
        return False
    return any(path.is_file() for path in summaries.rglob("*"))


NEXT_BAT_NAME = "transcribe_and_edit_next.bat"
BYPL_BAT_NAME = "transcribe_and_edit_next_bypl.bat"
BY_SUBSTR_BAT_NAME = "transcribe_and_edit_next_by_substr.bat"
REFRESH_SUMMARY_BAT_NAME = "refresh_summary.bat"
# Starting point for the title filter; meant to be edited before a run.
DEFAULT_TITLE_SUBSTR = {"ru": "Лекция №1.1."}
DEFAULT_TITLE_SUBSTR_FALLBACK = "Lecture 1.1."

BAT_TEMPLATE = r"""@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Generated by find_youtube_channel_by_descr.py. Edit if needed.
rem LANG is the original language of the channel videos (two-letter).
rem
rem Nothing else in this file needs editing. CHANNEL{extra_keys_note} live
rem in the sibling settings file next to this bat:
rem
rem     {settings_name}
rem
rem They are kept there rather than here because cmd.exe reads a bat in
rem the console code page and would turn a non-ASCII value into garbage
rem on its way into a variable; Python reads the settings file as UTF-8
rem instead. (Do not use chcp 65001 to get around that: it switches cmd
rem to a raster font and breaks Cyrillic on screen.)
set "LANG={lang}"
rem ====================================================================

cd /d {workspace}

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"

set "SETTINGS=%~dp0{settings_name}"
set "LOGDIR=%~dp0_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\{log_prefix}_%LOGSTAMP%.log"

rem The next video that is not fully edited yet is taken straight from
rem YouTube (only the audio is downloaded). Already transcribed but
rem unedited videos skip transcription and go straight to editing.
rem --orig-only: original language only; --annotate: 200-250 word
rem annotation .txt (RU original -> RU; otherwise EN + RU translation).
set "RUNCMD=python -u src\transcribe\transcribe_videos.py --settings '%SETTINGS%' --lang %LANG% --orig-only --from-youtube --edit --annotate {select_args}"

call :logecho === transcribe + final edit: {scope_note} ===
call :logecho Log file: %LOGFILE%
call :logecho Settings: %SETTINGS%
call :logecho Language: %LANG% (original only)
call :logblank
call :logecho Each stage (transcription, editing) shows its estimated cost
call :logecho and waits for a y/n confirmation.
call :logblank

call :logecho RUN: %RUNCMD%
powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); & %RUNCMD% 2>&1 | ForEach-Object {{ [Console]::WriteLine($_); Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }}; exit $LASTEXITCODE"
set "RUNEXIT=!ERRORLEVEL!"
if not "!RUNEXIT!"=="0" (
  echo ERROR: exit code !RUNEXIT!
  >>"%LOGFILE%" echo ERROR: exit code !RUNEXIT!
)

call :logblank
call :logecho === done ===
call :logecho Log saved: %LOGFILE%
echo.
pause
exit /b 0

:logecho
set "MSG=%*"
echo(!MSG!
>>"%LOGFILE%" echo(!MSG!
exit /b 0

:logblank
echo.
>>"%LOGFILE%" echo.
exit /b 0
"""

REFRESH_SUMMARY_BAT_TEMPLATE = r"""@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Generated by find_youtube_channel_by_descr.py. Edit if needed.
rem HANDLE is the YouTube @handle (get_summary / refresh_channel_cache
rem take that, not the folder path). CHANNEL is only for the log folder
rem label. LANG is the original language of the videos (two-letter) -
rem used for the transcription cost estimate in the summary.
rem
rem Use after a long pause: the video cache is force-refreshed from
rem YouTube, then the summary is rebuilt. Day-to-day updates do not need
rem this - get_summary_for_channel already refreshes the cache smartly.
rem
rem CHANNEL and HANDLE live in the sibling settings file next to this bat
rem (same reason as the other channel bats: cmd.exe and non-ASCII paths):
rem
rem     {settings_name}
set "LANG={lang}"
rem ====================================================================

cd /d {workspace}

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "OUTDATE=%%I"

set "SETTINGS=%~dp0{settings_name}"
set "LOGDIR=%~dp0_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\refresh_summary_%LOGSTAMP%.log"

rem HANDLE and CHANNEL come from the settings file; Python reads it as UTF-8.
set "REFRESHCMD=python -u src\channels\refresh_channel_cache.py --settings '%SETTINGS%' --force"
set "SUMMARYCMD=python -u src\channels\get_summary_for_channel.py --settings '%SETTINGS%' --lang %LANG% --include-playlist-only"

call :logecho === refresh video cache (force) and rebuild the channel summary ===
call :logecho Log file: %LOGFILE%
call :logecho Settings: %SETTINGS%
call :logecho Language:       %LANG% (for transcription cost estimate)
call :logecho Mode:           bypls (grouped by playlists)
call :logecho Flags:          refresh --force; summary --include-playlist-only
call :logecho Results:        see CHANNEL in the settings file; dated under _summaries\
call :logblank

call :logecho RUN: %REFRESHCMD%
powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); & %REFRESHCMD% 2>&1 | ForEach-Object {{ [Console]::WriteLine($_); Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }}; exit $LASTEXITCODE"
set "RUNEXIT=!ERRORLEVEL!"
if not "!RUNEXIT!"=="0" (
  echo ERROR: cache refresh exit code !RUNEXIT!
  >>"%LOGFILE%" echo ERROR: cache refresh exit code !RUNEXIT!
  call :logblank
  call :logecho === done with errors ===
  call :logecho Log saved: %LOGFILE%
  echo.
  pause
  exit /b !RUNEXIT!
)

call :logblank
call :logecho RUN: %SUMMARYCMD%
powershell -NoProfile -Command "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); & %SUMMARYCMD% 2>&1 | ForEach-Object {{ [Console]::WriteLine($_); Add-Content -LiteralPath '%LOGFILE%' -Value $_ -Encoding utf8 }}; exit $LASTEXITCODE"
set "RUNEXIT=!ERRORLEVEL!"
if not "!RUNEXIT!"=="0" (
  echo ERROR: summary exit code !RUNEXIT!
  >>"%LOGFILE%" echo ERROR: summary exit code !RUNEXIT!
)

call :logblank
call :logecho === done ===
call :logecho Log saved: %LOGFILE%
echo.
pause
exit /b 0

:logecho
set "MSG=%*"
echo(!MSG!
>>"%LOGFILE%" echo(!MSG!
exit /b 0

:logblank
echo.
>>"%LOGFILE%" echo.
exit /b 0
"""


def cached_channel_handle(channel_root: Path) -> str | None:
    """@handle from the channel caches, if either of them already has it."""
    for name in ("playlists.json", "videos.json"):
        path = channel_root / "_cache" / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        handle = str(data.get("channel_handle") or "").strip()
        if handle:
            return handle if handle.startswith("@") else f"@{handle}"
    return None


def default_playlist_folder(channel_root: Path) -> str | None:
    """Local playlist folder with the most videos (from the summary map).

    Videos outside any playlist (misc/) are ignored. Ties keep the first
    playlist in playlists.json order.
    """
    try:
        playlists = json.loads(
            (channel_root / "_cache" / "playlists.json").read_text(
                encoding="utf-8"
            )
        ).get("playlists") or []
    except (OSError, ValueError):
        playlists = []
    if not playlists:
        return None

    counts_by_title: dict[str, int] = {}
    try:
        mapping = json.loads(
            (channel_root / "_cache" / "video_playlists.json").read_text(
                encoding="utf-8"
            )
        ).get("map") or {}
        for title in mapping.values():
            if title:
                counts_by_title[str(title)] = (
                    counts_by_title.get(str(title), 0) + 1
                )
    except (OSError, ValueError):
        pass

    best_folder: str | None = None
    best_count = -1
    for playlist in playlists:
        folder = playlist.get("folder") or ""
        title = playlist.get("title") or ""
        if not folder:
            continue
        count = counts_by_title.get(title, 0)
        if count > best_count:
            best_count = count
            best_folder = folder
    if best_folder is not None and best_count > 0:
        return best_folder
    for playlist in playlists:
        folder = playlist.get("folder")
        if folder:
            return folder
    return None


def value_from_bat_or_settings(
    run_scripts: Path,
    bat_name: str,
    key: str,
    *,
    allowed: Collection[str],
    fallback: str = "",
) -> str:
    """Keep a hand-edited value when regenerating a bat and its settings."""
    settings_path = settings_beside(run_scripts / bat_name)
    if settings_path.is_file():
        try:
            value = read_settings(settings_path, allowed).get(key, "")
            if value:
                return value
        except SystemExit:
            pass
    bat_path = run_scripts / bat_name
    if not bat_path.is_file():
        return fallback
    try:
        text = bat_path.read_text(encoding="utf-8-sig")
    except OSError:
        return fallback
    patterns = (
        rf"(?im)^(?:rem\s+)?{re.escape(key)}=(.*)$",
        rf'(?im)^set\s+"{re.escape(key)}=(.*)"\s*$',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return fallback


def flat_settings_text(channel: str) -> str:
    return (
        "# Settings of transcribe_and_edit_next.bat - edit them here and run\n"
        "# the bat. Save this file as UTF-8; a line opening with '#' is a\n"
        "# comment, and of a repeated setting the first line is the one that\n"
        "# counts.\n"
        "\n"
        "# Channel folder relative to _channels\\.\n"
        f"CHANNEL={channel}\n"
    )


def by_substr_settings_text(channel: str, substr: str) -> str:
    return (
        "# Settings of transcribe_and_edit_next_by_substr.bat - edit them\n"
        "# here and run the bat. Save this file as UTF-8; a line opening with\n"
        "# '#' is a comment, and of a repeated setting the first line is the\n"
        "# one that counts.\n"
        "\n"
        "# Channel folder relative to _channels\\.\n"
        f"CHANNEL={channel}\n"
        "\n"
        "# Only the videos whose YouTube title contains that text are\n"
        "# processed (case-insensitive, the whole channel, playlists\n"
        "# ignored). There may be several matches; each one asks for its\n"
        "# own confirmation, and 'n' moves on to the next match.\n"
        f"TITLE_SUBSTR={substr}\n"
    )


def bypl_settings_text(channel: str, playlist: str) -> str:
    return (
        "# Settings of transcribe_and_edit_next_bypl.bat - edit them here\n"
        "# and run the bat. Save this file as UTF-8; a line opening with '#'\n"
        "# is a comment, and of a repeated setting the first line is the one\n"
        "# that counts.\n"
        "\n"
        "# Channel folder relative to _channels\\.\n"
        f"CHANNEL={channel}\n"
        "\n"
        "# Local folder under _playlists/; default is the playlist with the\n"
        "# most videos. Edit before a run to switch playlist.\n"
        f"PLAYLIST={playlist}\n"
    )


def refresh_settings_text(channel: str, handle: str) -> str:
    return (
        "# Settings of refresh_summary.bat - edit them here and run the bat.\n"
        "# Save this file as UTF-8; a line opening with '#' is a comment, and\n"
        "# of a repeated setting the first line is the one that counts.\n"
        "\n"
        "# Channel folder relative to _channels\\ (for logs and regrouping).\n"
        f"CHANNEL={channel}\n"
        "\n"
        "# YouTube @handle - get_summary / refresh_channel_cache take this,\n"
        "# not the folder path.\n"
        f"HANDLE={handle}\n"
    )


def generate_channel_bats(
    channel_root: Path,
    lang: str,
    *,
    overwrite_bypl: bool = False,
    overwrite: bool = False,
    only: Collection[str] | None = None,
) -> None:
    """Write the channel automation bats and their sibling settings files.

    `only` limits the work to some of them (used to backfill a newly added
    bat into existing channels). Existing bats are kept unless `overwrite`
    (or `overwrite_bypl` for the playlist bat) is set; settings files are
    always refreshed so CHANNEL stays current, while TITLE_SUBSTR / PLAYLIST
    / HANDLE keep any hand-edited value.
    """
    run_scripts = channel_root / "_run_scripts"
    run_scripts.mkdir(parents=True, exist_ok=True)
    channel = channel_relative_ref(channel_root, channels_dir(WORKSPACE_ROOT))
    workspace = str(WORKSPACE_ROOT)
    playlist_folder = default_playlist_folder(channel_root)
    handle = cached_channel_handle(channel_root)

    substr = value_from_bat_or_settings(
        run_scripts,
        BY_SUBSTR_BAT_NAME,
        "TITLE_SUBSTR",
        allowed=("CHANNEL", "TITLE_SUBSTR"),
        fallback=DEFAULT_TITLE_SUBSTR.get(lang, DEFAULT_TITLE_SUBSTR_FALLBACK),
    )
    playlist_value = value_from_bat_or_settings(
        run_scripts,
        BYPL_BAT_NAME,
        "PLAYLIST",
        allowed=("CHANNEL", "PLAYLIST"),
        fallback=playlist_folder or "",
    )
    handle_value = value_from_bat_or_settings(
        run_scripts,
        REFRESH_SUMMARY_BAT_NAME,
        "HANDLE",
        allowed=("CHANNEL", "HANDLE"),
        fallback=handle or "",
    )

    flat_name = "transcribe_and_edit_next.settings.txt"
    flat = BAT_TEMPLATE.format(
        lang=lang,
        workspace=workspace,
        settings_name=flat_name,
        extra_keys_note="",
        select_args="--next 1",
        log_prefix="transcribe_and_edit_next",
        scope_note="next video from the flat channel list",
    )
    flat_settings = flat_settings_text(channel)

    substr_name = "transcribe_and_edit_next_by_substr.settings.txt"
    by_substr = BAT_TEMPLATE.format(
        lang=lang,
        workspace=workspace,
        settings_name=substr_name,
        extra_keys_note=" and TITLE_SUBSTR",
        select_args="--next all",
        log_prefix="transcribe_and_edit_next_by_substr",
        scope_note="every channel video whose title contains TITLE_SUBSTR",
    )
    by_substr_settings = by_substr_settings_text(channel, substr)

    bypl: str | None = None
    bypl_settings: str | None = None
    bypl_name = "transcribe_and_edit_next_bypl.settings.txt"
    if playlist_value and (only is None or BYPL_BAT_NAME in only):
        bypl = BAT_TEMPLATE.format(
            lang=lang,
            workspace=workspace,
            settings_name=bypl_name,
            extra_keys_note=" and PLAYLIST",
            select_args="--next 1",
            log_prefix="transcribe_and_edit_next_bypl",
            scope_note="next video from the playlist named in PLAYLIST",
        )
        bypl_settings = bypl_settings_text(channel, playlist_value)
    elif only is None or BYPL_BAT_NAME in only:
        print(
            "  WARNING: no playlist folders in _cache/playlists.json; "
            f"{BYPL_BAT_NAME} was not generated.",
            flush=True,
        )

    refresh_summary: str | None = None
    refresh_settings: str | None = None
    refresh_name = "refresh_summary.settings.txt"
    if only is None or REFRESH_SUMMARY_BAT_NAME in only:
        if handle_value:
            refresh_summary = REFRESH_SUMMARY_BAT_TEMPLATE.format(
                lang=lang,
                workspace=workspace,
                settings_name=refresh_name,
            )
            refresh_settings = refresh_settings_text(channel, handle_value)
        else:
            print(
                "  WARNING: no channel_handle in _cache/; "
                f"{REFRESH_SUMMARY_BAT_NAME} was not generated.",
                flush=True,
            )

    for name, content, settings_body, force in (
        (NEXT_BAT_NAME, flat, flat_settings, overwrite),
        (BY_SUBSTR_BAT_NAME, by_substr, by_substr_settings, overwrite),
        (BYPL_BAT_NAME, bypl, bypl_settings, overwrite or overwrite_bypl),
        (
            REFRESH_SUMMARY_BAT_NAME,
            refresh_summary,
            refresh_settings,
            overwrite,
        ),
    ):
        if content is None or (only is not None and name not in only):
            continue
        path = run_scripts / name
        settings_path = settings_beside(path)
        # Settings always refresh CHANNEL (and keep edited keys via the
        # helpers above). The bat is rewritten when missing, forced, or still
        # on the old rem/set scheme so existing channels pick up the change.
        bat_text = ""
        if path.is_file():
            try:
                bat_text = path.read_text(
                    encoding="utf-8-sig", errors="replace"
                )
            except OSError:
                bat_text = ""
        old_style = bool(bat_text) and (
            "rem TITLE_SUBSTR=" in bat_text
            or 'set "CHANNEL=' in bat_text
            or 'set "PLAYLIST=' in bat_text
            or 'set "HANDLE=' in bat_text
        )
        settings_existed = settings_path.is_file()
        write_settings(settings_path, settings_body)
        print(
            f"  {'Updated' if settings_existed else 'Created'}: "
            f"{settings_path.relative_to(channel_root)}",
            flush=True,
        )
        if path.exists() and not force and not old_style:
            print(f"  Kept the existing {path.relative_to(channel_root)}",
                  flush=True)
            continue
        existed = path.exists()
        path.write_bytes(content.replace("\n", "\r\n").encode("utf-8"))
        note = ""
        if name == BYPL_BAT_NAME:
            note = f" (PLAYLIST={playlist_value})"
        elif name == BY_SUBSTR_BAT_NAME:
            note = f" (TITLE_SUBSTR={substr})"
        elif name == REFRESH_SUMMARY_BAT_NAME:
            note = f" (HANDLE={handle_value})"
        print(
            f"  {'Updated' if existed else 'Created'}: "
            f"{path.relative_to(channel_root)}{note}",
            flush=True,
        )


def ensure_channel_layout(channel_root: Path) -> None:
    for name in ("_cache", "_playlists", "_run_scripts", "_summaries"):
        (channel_root / name).mkdir(parents=True, exist_ok=True)


def format_followers(count) -> str:
    if not isinstance(count, (int, float)):
        return "?"
    return f"{int(count):,}".replace(",", " ")


def print_candidate(index: int, cand: dict) -> None:
    print(f"--- Candidate {index} ---", flush=True)
    print(f"  Channel:     {cand['title']}", flush=True)
    handle = cand.get("handle") or "?"
    print(f"  Handle:      {handle}   ({cand['url']})", flush=True)
    print(f"  Subscribers: {format_followers(cand.get('followers'))}",
          flush=True)
    if cand.get("description"):
        print(f"  About:       {cand['description'][:200]}", flush=True)
    if cand.get("via_video"):
        print(f"  Found via the video: {cand['via_video'][:120]}", flush=True)
    total = cand.get("total_videos")
    print(f"  Videos:      {total if total is not None else '?'} total",
          flush=True)
    if cand.get("language"):
        print(f"  Content language (guess): {cand['language']}", flush=True)
    playlists = cand.get("playlists") or []
    print(f"  Playlists:   {len(playlists)}", flush=True)
    for pl, translation in zip(playlists, cand.get("translations") or []):
        count = pl.get("count")
        line = f"    - {pl['title']} - {count if count is not None else '?'} video(s)"
        if translation:
            line += f"  [RU: {translation}]"
        print(line, flush=True)
    existing = cand.get("existing_root")
    if existing is not None:
        note = (
            "summaries already exist"
            if cand.get("has_summaries")
            else "no summaries yet"
        )
        print(
            f"  Local folder: _channels\\{channel_relative_ref(existing, channels_dir(WORKSPACE_ROOT))} "
            f"already exists "
            f"({note}).",
            flush=True,
        )
    else:
        print("  Local folder: none yet (will be created).", flush=True)
    print("", flush=True)


def ask_choice(prompt: str, options: set[str]) -> str:
    while True:
        print(prompt, flush=True)
        try:
            answer = input().strip().lower()
        except EOFError:
            return "q"
        if answer in options:
            return answer
        print(f"  Please answer one of: {', '.join(sorted(options))}",
              flush=True)


def run_summary_for(
    cand: dict, scope: str, lang: str, container: str | None
) -> int:
    from channels import get_summary_for_channel
    ref = cand.get("handle") or cand["id"]
    argv = [ref, scope, "--lang", lang]
    if container:
        argv += ["--container", container]
    print("", flush=True)
    print(
        f"=== Summary: {cand['title']} ({ref}, {scope}, lang={lang}) ===",
        flush=True,
    )
    return get_summary_for_channel.main(argv)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find a YouTube channel by a free-text description."
    )
    parser.add_argument(
        "description",
        nargs="?",
        default=None,
        help=(
            "Free-text channel description, e.g. "
            '"Политолог Аббас Галлямов YouTube"; may come from --settings '
            "instead, and overrides it when both are given"
        ),
    )
    parser.add_argument(
        "--lang",
        default=None,
        metavar="XX",
        help=(
            "Original language of the channel videos (two-letter code); "
            "default: guessed from the channel metadata, fallback ru"
        ),
    )
    parser.add_argument(
        "--container",
        default=None,
        metavar="FOLDER",
        help=(
            "Folder under _channels/ to create the channel folder in, e.g. "
            'IT\\Dot.Net (the CHANNEL_PATH setting of the bat). A channel '
            "that already has a folder keeps it"
        ),
    )
    parser.add_argument(
        "--settings",
        default=None,
        metavar="FILE",
        type=Path,
        help=(
            "Text file with the DESCR and CHANNEL_PATH lines to run with; "
            "this is how the bat passes a description that cmd.exe cannot "
            "carry itself. Command-line values win over it"
        ),
    )
    return parser.parse_args(argv)


def what_to_search_for(args: argparse.Namespace) -> tuple[str, str | None]:
    """The description and the container folder of this run.

    Both may come from the command line or from the settings file the bat
    points at; a value given on the command line wins.
    """
    description = (args.description or "").strip()
    container = (args.container or "").strip().strip("\\/") or None
    if args.settings:
        settings = read_settings(args.settings, SETTINGS_KEYS)
        print(f"Settings: {args.settings}", flush=True)
        if not description:
            description = settings.get("DESCR", "")
        if container is None:
            container = settings.get("CHANNEL_PATH", "").strip("\\/") or None
    if not description:
        where = (
            f"Set DESCR in {args.settings}"
            if args.settings
            else "Pass it as the first argument"
        )
        raise SystemExit(f"The channel description is empty. {where}.")
    print(f"Description: {description}", flush=True)
    goes_into = f"_channels\\{container}" if container else "_channels\\ itself"
    print(f"Channel folder goes into: {goes_into}", flush=True)
    return description, container


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    description, container = what_to_search_for(args)

    try:
        from transcribe.transcribe_videos import read_api_key

        api_key: str | None = read_api_key(WORKSPACE_ROOT)
    except SystemExit:
        api_key = None
        print(
            "NOTE: no OPENAI_API_KEY - playlist titles will not be "
            "translated into Russian.",
            flush=True,
        )

    print(f'Searching YouTube for channels: "{description}"...', flush=True)
    candidates = search_channels(description)
    if not candidates:
        print("No channels found for this description.", flush=True)
        return 1
    print(
        f"Found {len(candidates)} channel(s), in decreasing relevance "
        "order. Gathering details...",
        flush=True,
    )

    for cand in candidates:
        print(f"  ... {cand['title']}", flush=True)
        cand["total_videos"] = fetch_uploads_count(cand["id"])
        try:
            cand["playlists"] = fetch_playlists_with_counts(cand["id"])
        except Exception as exc:  # noqa: BLE001 - info display is best-effort
            print(f"  WARNING: could not fetch playlists: {exc}", flush=True)
            cand["playlists"] = []
        needs_translation = any(
            not looks_russian_or_translit(pl["title"])
            for pl in cand["playlists"]
        )
        if cand["playlists"] and (needs_translation or args.lang is None):
            cand["language"], cand["translations"] = translate_playlists(
                api_key, cand, cand["playlists"]
            )
        else:
            cand["language"], cand["translations"] = (
                None, [None] * len(cand["playlists"])
            )
        cand["existing_root"] = locate_channel_root(
            cand["id"], cand.get("handle")
        )
        cand["has_summaries"] = (
            summaries_present(cand["existing_root"])
            if cand["existing_root"] is not None
            else False
        )

    print("", flush=True)
    for index, cand in enumerate(candidates, start=1):
        print_candidate(index, cand)

    processed_any = False
    for index, cand in enumerate(candidates, start=1):
        existing_note = ""
        if cand["existing_root"] is not None:
            existing_note = (
                " (folder exists, summaries already present)"
                if cand["has_summaries"]
                else " (folder exists, no summaries yet)"
            )
        answer = ask_choice(
            f"[{index}/{len(candidates)}] Build the summary for "
            f'"{cand["title"]}"{existing_note}? (y/n/q)',
            {"y", "yes", "n", "no", "q"},
        )
        if answer == "q":
            print("Quit.", flush=True)
            break
        if answer in ("n", "no"):
            continue

        if cand.get("playlists"):
            grouping = ask_choice(
                "  Group the summary by playlists? "
                "(1 = by playlists [bypls], 2 = flat list [allpls], q = quit)",
                {"1", "2", "q"},
            )
            if grouping == "q":
                print("Quit.", flush=True)
                break
            scope = "bypls" if grouping == "1" else "allpls"
        else:
            # Nothing to group by: the channel keeps no playlists, and all
            # of its videos will live in the one misc folder.
            print("  This channel has no playlists: a flat list it is.",
                  flush=True)
            scope = "allpls"
        lang = (args.lang or cand.get("language") or "ru").lower()

        code = run_summary_for(cand, scope, lang, container)
        if code:
            print(f"Summary script exited with code {code}.", flush=True)
            return code

        channel_root = locate_channel_root(cand["id"], cand.get("handle"))
        if channel_root is None:
            print(
                "WARNING: the channel folder was not found after the "
                "summary run; bat files were not generated.",
                flush=True,
            )
            continue
        ensure_channel_layout(channel_root)
        print(f"Channel folder: {channel_root}", flush=True)
        generate_channel_bats(channel_root, lang)
        processed_any = True

    print("", flush=True)
    print(
        "Done." if processed_any else "Done (no summaries were built).",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
