#!/usr/bin/env python3
"""Step 2 of the pipeline: download channel videos with yt-dlp.

Until now this step was manual (a hand-written bat with a list of URLs).
The script takes the video list the summary stage already built, downloads
the videos that are still missing and names them the way the rest of the
pipeline expects: <playlist folder>/NN_<title> [<video id>].mp4, so
transcribe_videos.py and extract_slides.py pick them up as local files.

Downloading costs nothing: yt-dlp talks to YouTube directly, no API key and
no paid service is involved. The only budget here is disk space and time.

With a playlist folder the videos come from that YouTube playlist, in
playlist order. Without it the flat channel-wide list is used (newest
first), and every video goes into the folder of its own playlist, exactly
like the flat mode of transcribe_videos.py; videos outside any playlist go
to misc/.

A video counts as downloaded when the folder already holds a media file
with its [video id] marker, so an interrupted session simply continues on
the next run, and --next 1 walks the list one video per run.

Right after a video is downloaded the comments under it are read for a
presentation the lecturer may have linked, and if there is one it is
offered for download into PRESENTATIONS/ (see download_presentations.py).
--no-presentations skips that lookup; for videos downloaded before this
step existed there is extract_slides.py --presentations.

Usage:
  python src/download_videos.py Game_Design\\_makingitright9305 \
      kurs_geym_dizayna_nri_making_it_right --next 1
  python src/download_videos.py Game_Design\\_makingitright9305 \
      kurs_geym_dizayna_nri_making_it_right --next all

Automation: _channels/<channel>/_run_scripts/download_videos_next.bat and
download_videos_all.bat.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from download_presentations import offer_presentation
from project_paths import WORKSPACE_ROOT, channels_dir, require_channel_ref
from transcribe_videos import (
    PauseWatcher,
    duration_to_text,
    fetch_playlist_entries,
    load_channel_flat_jobs,
    load_playlist_meta,
    load_unlisted_playlist_entries,
    local_media_by_id,
    next_label,
    normalize_next_count,
    remote_stem,
    stem_budget,
)
from yt_dlp_opts import cookie_args, youtube_media_args

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Progress lines only every few seconds: a bat tees its output into a log,
# and yt-dlp would otherwise write hundreds of lines per video.
PROGRESS_DELTA_SECONDS = "5"
DEFAULT_MAX_HEIGHT = 1080


def format_selector(max_height: int) -> str:
    """yt-dlp -f value: mp4 where possible, capped by height."""
    if max_height <= 0:
        return "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
    cap = f"[height<={max_height}]"
    return (
        f"bv*{cap}[ext=mp4]+ba[ext=m4a]/"
        f"bv*{cap}+ba/"
        f"b{cap}/b"
    )


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def download_video(
    job: dict, playlist_dir: Path, stem: str, args: argparse.Namespace
) -> tuple[bool, bool]:
    """Run yt-dlp for one video, streaming its output.

    Returns (yt-dlp exited cleanly, YouTube demanded a sign-in). The second
    flag ends the session: that block is per IP or per account, so the next
    videos would only collect the same refusal.
    """
    # yt-dlp reads % as the start of a field in the output template.
    template = str(playlist_dir / f"{stem.replace('%', '%%')}.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist",
        "-f", format_selector(args.max_height),
        "--merge-output-format", "mp4",
        "--newline",
        "--progress-delta", PROGRESS_DELTA_SECONDS,
        "-o", template,
    ]
    cmd += youtube_media_args(args.cookies, args.cookies_from_browser)
    cmd.append(job["url"])

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    blocked = False
    for line in process.stdout:
        line = line.rstrip()
        if not line:
            continue
        if "confirm you" in line and "not a bot" in line:
            blocked = True
        print(f"    {line}", flush=True)
    return process.wait() == 0, blocked


def collect_jobs(
    args: argparse.Namespace, channel_dir: Path
) -> tuple[list[dict], str]:
    """(jobs, scope note) for the playlist or for the whole channel."""
    if args.playlist_folder is None:
        jobs, channel_name = load_channel_flat_jobs(channel_dir)
        print(
            f'Channel: "{channel_name}" - flat video list '
            f"({len(jobs)} videos, newest first).",
            flush=True,
        )
        return jobs, "in the channel"

    meta = load_playlist_meta(channel_dir, args.playlist_folder)
    print(f'YouTube playlist: "{meta["title"]}" ({meta["id"]})', flush=True)
    playlist_id = str(meta["id"] or "")
    if playlist_id.startswith("unlisted:"):
        print("Loading the hand-written unlisted series...", flush=True)
        raw_entries = load_unlisted_playlist_entries(
            channel_dir, args.playlist_folder
        )
    else:
        print("Fetching the playlist entry list...", flush=True)
        raw_entries = fetch_playlist_entries(playlist_id)
    jobs = [
        {**entry, "folder": args.playlist_folder}
        for entry in raw_entries
    ]
    return jobs, "in the playlist"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download channel videos with yt-dlp (free, no API key)."
    )
    parser.add_argument(
        "channel_folder",
        help="Channel ref under _channels/ (e.g. _Autotesting or "
        "Game_Design\\_makingitright9305)",
    )
    parser.add_argument(
        "playlist_folder",
        nargs="?",
        default=None,
        help=(
            "Playlist folder name under <channel>/_playlists. Omit it to "
            "walk the flat channel-wide list instead, each video going to "
            "the folder of its own playlist"
        ),
    )
    parser.add_argument(
        "--next",
        dest="next_count",
        default="1",
        metavar="N",
        help=(
            "How many videos to download this session (default: 1); "
            "'all' takes every video that is still missing"
        ),
    )
    parser.add_argument(
        "--title-substr",
        dest="title_substr",
        default=None,
        metavar="TEXT",
        help="Only the videos whose title contains this text (any case)",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=DEFAULT_MAX_HEIGHT,
        metavar="PX",
        help=(
            f"Cap the video height (default: {DEFAULT_MAX_HEIGHT}); the "
            "pipeline never needs more, and 4K would cost a lot of disk. "
            "0 means the best available quality"
        ),
    )
    parser.add_argument(
        "--cookies-from-browser",
        default=None,
        metavar="BROWSER",
        help=(
            "Take YouTube cookies from this browser (firefox / chrome / "
            "edge); needed when YouTube asks to confirm you are not a bot. "
            "The browser must be closed, otherwise it locks its cookie "
            "database"
        ),
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "cookies.txt exported from a browser (Netscape format); takes "
            "precedence over --cookies-from-browser"
        ),
    )
    parser.add_argument(
        "--no-presentations",
        action="store_true",
        help=(
            "Do not look for the presentation the lecturer may have linked "
            "in a comment under the video (see download_presentations.py); "
            "by default every downloaded video is checked"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=WORKSPACE_ROOT,
        help="Workspace root (default: parent of src/)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.next_count = normalize_next_count(args.next_count)
    channel_dir = require_channel_ref(
        channels_dir(args.workspace), args.channel_folder
    )
    print(f"Channel folder: {channel_dir}", flush=True)

    jobs, scope_note = collect_jobs(args, channel_dir)
    if args.title_substr:
        needle = args.title_substr.casefold()
        matching = [job for job in jobs if needle in job["title"].casefold()]
        print(
            f'Title filter "{args.title_substr}": {len(matching)} of '
            f"{len(jobs)} video title(s) match.",
            flush=True,
        )
        jobs = matching
        scope_note += " matching the title filter"

    playlists_root = channel_dir / "_playlists"
    downloaded_cache: dict[str, dict[str, Path]] = {}
    pending: list[dict] = []
    for job in jobs:
        folder = job["folder"]
        if folder not in downloaded_cache:
            (playlists_root / folder).mkdir(parents=True, exist_ok=True)
            downloaded_cache[folder] = local_media_by_id(playlists_root / folder)
        if job["id"] not in downloaded_cache[folder]:
            pending.append(job)

    session = pending if args.next_count is None else pending[: args.next_count]
    print(
        f"Videos: {len(jobs)} {scope_note}, {len(jobs) - len(pending)} already "
        f"downloaded, {len(session)} in this session "
        f"(--next {next_label(args.next_count)}).",
        flush=True,
    )
    quality = (
        "best available"
        if args.max_height <= 0
        else f"up to {args.max_height}p"
    )
    print(f"Quality: {quality} (--max-height), mp4; downloading is free.",
          flush=True)
    if not session:
        print("Nothing to do: every video is already downloaded.", flush=True)
        return 0

    for position, job in enumerate(session, start=1):
        duration = duration_to_text(job.get("duration"))
        suffix = f"  ({duration})" if duration else ""
        print(f"  {position:2d}. {job['title']}{suffix}", flush=True)
    if not args.yes:
        print(f"Download {len(session)} video(s)? (y/n)", flush=True)
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            print("Session stopped: download not confirmed.", flush=True)
            return 0
    print("Press 'p' to stop after the current video.", flush=True)

    watcher = PauseWatcher()
    processed = 0
    failed: list[str] = []
    total_bytes = 0
    presentations = 0
    looking_for_presentations = not args.no_presentations
    for position, job in enumerate(session, start=1):
        playlist_dir = playlists_root / job["folder"]
        print(
            f"[{position}/{len(session)}] {job['title']}  "
            f"[{job['folder']}]",
            flush=True,
        )
        stem = remote_stem(
            job.get("index") or 0,
            job["title"],
            job["id"],
            max_len=stem_budget(playlist_dir),
        )
        ok, blocked = download_video(job, playlist_dir, stem, args)
        saved = local_media_by_id(playlist_dir).get(job["id"]) if ok else None
        if blocked:
            print(
                "  YouTube is asking to confirm you are not a bot, so the "
                "session stops here: the block is per IP or per account, "
                "and the remaining videos would only repeat it.",
                flush=True,
            )
            print(
                "  Pass cookies of a signed-in browser "
                "(--cookies-from-browser firefox, or the COOKIES line in the "
                "bat), or try again later.",
                flush=True,
            )
            failed.append(job["title"])
            break
        if saved is None:
            print("  WARNING: the video was not downloaded.", flush=True)
            failed.append(job["title"])
        else:
            size = saved.stat().st_size
            total_bytes += size
            processed += 1
            print(f"  Saved: {saved.name} ({human_size(size)})", flush=True)
            if looking_for_presentations:
                found, refused = offer_presentation(
                    playlist_dir,
                    saved.stem,
                    cookies=cookie_args(
                        args.cookies, args.cookies_from_browser
                    ),
                    auto_yes=args.yes,
                )
                if found is not None:
                    presentations += 1
                # One refusal is enough: it is per IP or per account, and
                # the next lookup would only collect the same answer.
                looking_for_presentations = not refused
        if watcher.pause_requested() and position < len(session):
            print("Pause requested: stopping after the current video.",
                  flush=True)
            break

    print(
        f"Session done: {processed} video(s), {human_size(total_bytes)}"
        + (f", {presentations} presentation(s)." if presentations else "."),
        flush=True,
    )
    if failed:
        print(f"Failed: {len(failed)} video(s).", flush=True)
        for title in failed:
            print(f"  - {title}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
