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
_run_scripts, _summaries) and generates two automation bat files in the
channel's _run_scripts:

- transcribe_and_edit_next.bat      - transcribe + edit the next
  untranscribed (or transcribed-but-unedited) video from the flat
  channel-wide video list;
- transcribe_and_edit_next_bypl.bat - the same for the playlist whose
  folder name is passed as the bat parameter.

Both bats use transcribe_videos.py --from-youtube --edit --annotate
--orig-only, so the edited documents are produced in the original language
only, with 200-250 word annotations (Russian original -> Russian
annotation; other originals -> English + Russian annotations).

Usage:
  python src/find_youtube_channel_by_descr.py "Политолог Аббас Галлямов YouTube"

Automation: _run_scripts/find_youtube_channel_by_descr.bat (project root);
the search description is set in a clearly marked line of the bat file.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from channel_browse import (
    client_context,
    extract_lockups,
    find_continuations,
    post_innertube,
)
from project_paths import WORKSPACE_ROOT, channel_folder_name, channels_dir

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MAX_CANDIDATES = 3
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
    from text_from_slides import chat_json

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
    if not root.is_dir():
        return None
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        for cache_name in ("playlists.json", "videos.json"):
            try:
                data = json.loads(
                    (child / "_cache" / cache_name).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if data.get("channel_id") == channel_id:
                return child
    if handle:
        guess = root / channel_folder_name(handle)
        if guess.is_dir():
            return guess
    return None


def summaries_present(channel_root: Path) -> bool:
    summaries = channel_root / "_summaries"
    if not summaries.is_dir():
        return False
    return any(path.is_file() for path in summaries.rglob("*"))


NEXT_BAT_NAME = "transcribe_and_edit_next.bat"
BYPL_BAT_NAME = "transcribe_and_edit_next_bypl.bat"

BAT_TEMPLATE = r"""@echo off
setlocal EnableDelayedExpansion

rem ====================================================================
rem Generated by find_youtube_channel_by_descr.py. Edit if needed.
rem LANG is the original language of the channel videos (two-letter).
set "CHANNEL={channel}"
set "LANG={lang}"
rem ====================================================================
{playlist_block}
cd /d {workspace}

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH_mm"') do set "LOGSTAMP=%%I"

set "LOGDIR=_channels\%CHANNEL%\_run_scripts\_logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOGFILE=%LOGDIR%\{log_prefix}_%LOGSTAMP%.log"

rem The next video that is not fully edited yet is taken straight from
rem YouTube (only the audio is downloaded). Already transcribed but
rem unedited videos skip transcription and go straight to editing.
rem --orig-only: original language only; --annotate: 200-250 word
rem annotation .txt (RU original -> RU; otherwise EN + RU translation).
set "RUNCMD=python -u src\transcribe_videos.py %CHANNEL% {playlist_arg}--lang %LANG% --orig-only --from-youtube --edit --annotate --next 1"

call :logecho === transcribe + final edit: {scope_note} ===
call :logecho Log file: %LOGFILE%
call :logecho Channel folder: %CHANNEL%
{playlist_echo}call :logecho Language: %LANG% (original only)
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

BYPL_PLAYLIST_BLOCK = r"""
rem The playlist folder (under _channels\%CHANNEL%\_playlists) is the
rem first parameter of this bat file.
if "%~1"=="" (
  echo Usage: %~nx0 ^<playlist_folder^>
  echo Playlist folders live under _channels\%CHANNEL%\_playlists\
  pause
  exit /b 1
)
set "PLAYLIST=%~1"
"""


def generate_channel_bats(channel_root: Path, lang: str) -> None:
    run_scripts = channel_root / "_run_scripts"
    run_scripts.mkdir(parents=True, exist_ok=True)
    channel = channel_root.name
    workspace = str(WORKSPACE_ROOT)

    flat = BAT_TEMPLATE.format(
        channel=channel,
        lang=lang,
        workspace=workspace,
        playlist_block="",
        playlist_arg="",
        playlist_echo="",
        log_prefix="transcribe_and_edit_next",
        scope_note="next video from the flat channel list",
    )
    bypl = BAT_TEMPLATE.format(
        channel=channel,
        lang=lang,
        workspace=workspace,
        playlist_block=BYPL_PLAYLIST_BLOCK,
        playlist_arg="%PLAYLIST% ",
        playlist_echo="call :logecho Playlist folder: %PLAYLIST%\n",
        log_prefix="transcribe_and_edit_next_bypl",
        scope_note="next video from the given playlist",
    )
    for name, content in ((NEXT_BAT_NAME, flat), (BYPL_BAT_NAME, bypl)):
        path = run_scripts / name
        if path.exists():
            print(f"  Kept the existing {path.relative_to(channel_root)}",
                  flush=True)
            continue
        path.write_text(
            content.replace("\n", "\r\n"), encoding="ascii", errors="replace"
        )
        print(f"  Created: {path.relative_to(channel_root)}", flush=True)


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
            f"  Local folder: _channels\\{existing.name} already exists "
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


def run_summary_for(cand: dict, scope: str, lang: str) -> int:
    import get_summary_for_channel

    ref = cand.get("handle") or cand["id"]
    argv = [ref, scope, "--lang", lang]
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
        help='Free-text channel description, e.g. "Политолог Аббас Галлямов YouTube"',
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    description = args.description.strip()
    if not description:
        raise SystemExit("The channel description must not be empty.")

    try:
        from transcribe_videos import read_api_key

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

        grouping = ask_choice(
            "  Group the summary by playlists? "
            "(1 = by playlists [bypls], 2 = flat list [allpls], q = quit)",
            {"1", "2", "q"},
        )
        if grouping == "q":
            print("Quit.", flush=True)
            break
        scope = "bypls" if grouping == "1" else "allpls"
        lang = (args.lang or cand.get("language") or "ru").lower()

        code = run_summary_for(cand, scope, lang)
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
