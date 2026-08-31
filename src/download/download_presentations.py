#!/usr/bin/env python3
"""Step 2a of the pipeline: download the presentation linked under a video.

Some lecturers hand out the slides in a comment under their own lecture:
"Презентация: <link to Google Drive>", often with other materials next to
it. That file is what the video shows on screen, so it belongs next to the
video: the slide stages then have something to be checked against, and the
final document can lean on the real slide text instead of what an OCR pass
made out of a screen share.

The link is not simply "the first comment": viewers' comments float above
the lecturer's, and on a popular video the lecturer's may be far down the
list. So the search goes by author - the comments of the video's own
uploader - and inside them by label: the line that says "презентация"
before its link. Whatever else the uploader linked (schemes, documents) is
written down in the sidecar but not downloaded; the presentation is what
this step is about.

Files land in <playlist>/PRESENTATIONS/<stem>.<ext>, named after the video
file exactly as INFO/<stem>.json is, with <stem>.json beside them recording
where the file came from and what else was on offer.

Google Drive files shared before 2021 need their resourcekey to be readable
without signing in. It comes as part of the link and is passed on to the
download, which is why an old link that looks dead in a browser still works
here.

Downloading costs nothing: no API key, no paid service.

Usage:
  python src/download/download_presentations.py Game_Design\\_makingitright9305 \
      kurs_geym_dizayna_nri_making_it_right --next all
  python src/download/download_presentations.py Game_Design\\_makingitright9305 \
      kurs_geym_dizayna_nri_making_it_right --video "04_Making It Right..."

The step is also chained from download_videos.py, which offers to fetch the
presentation right after it downloads a video, and from extract_slides.py
--presentations, for videos downloaded before this step existed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from shared.project_paths import (
    WORKSPACE_ROOT,
    channels_dir,
    require_channel_ref,
)
from transcribe.transcribe_videos import (
    list_videos,
    next_label,
    normalize_next_count,
    run_tool,
    watch_url,
)
from shared.yt_dlp_opts import YOUTUBE_SYSTEM_CERTS, cookie_args

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PRESENTATIONS_DIRNAME = "PRESENTATIONS"
SIDECAR_SUFFIX = ".json"
# Top-level comments to look through. The lecturer's comment is usually
# pinned or old and well liked, so the top sort brings it up early; the cap
# keeps a video with thousands of comments from turning into a long crawl.
MAX_COMMENTS = 200
REQUEST_TIMEOUT = 120
CHUNK = 1 << 16
USER_AGENT = "Mozilla/5.0 (Streams_from_Youtube_Channels pipeline)"

# The label in front of the link, on the lecturer's own line.
PRESENTATION_LABEL = re.compile(
    r"презентац|слайд|presentation|slide", re.IGNORECASE
)
LABELLED_LINK = re.compile(
    r"^[ \t>*-]*(?P<label>[^\n:]{0,80}?)\s*:\s*(?P<url>https?://\S+)",
    re.MULTILINE,
)
ANY_LINK = re.compile(r"https?://\S+")

DRIVE_FILE = re.compile(r"drive\.google\.com/file/d/([\w-]+)")
DRIVE_BY_ID = re.compile(r"drive\.google\.com/\S*[?&]id=([\w-]+)")
DOCS_FILE = re.compile(
    r"docs\.google\.com/(document|presentation|spreadsheets)/d/([\w-]+)"
)
RESOURCE_KEY = re.compile(r"resourcekey=([\w-]+)")
ID_IN_STEM = re.compile(r"\[([\w-]{11})\]$")
# What Google Docs editors export as, when a link points at an editor
# rather than at an uploaded file.
DOCS_EXPORT = {
    "document": "export?format=pdf",
    "presentation": "export/pdf",
    "spreadsheets": "export?format=pdf",
}
PPTX = (
    "application/vnd.openxmlformats-officedocument"
    ".presentationml.presentation"
)
CONTENT_TYPE_SUFFIX = {
    "application/pdf": ".pdf",
    "application/vnd.ms-powerpoint": ".ppt",
    PPTX: ".pptx",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/zip": ".zip",
}


def video_id_from_stem(stem: str) -> str | None:
    """The [video id] marker the download step leaves in the file name."""
    match = ID_IN_STEM.search(stem)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Finding the link


def fetch_comments(
    video_id: str, *, cookies: list[str]
) -> tuple[list[dict], bool]:
    """(comments, YouTube demanded a sign-in) for one video, via yt-dlp."""
    result = run_tool([
        sys.executable, "-m", "yt_dlp",
        *YOUTUBE_SYSTEM_CERTS,
        "-J",
        "--no-playlist",
        "--skip-download",
        "--write-comments",
        "--extractor-args",
        "youtube:comment_sort=top;"
        f"max_comments={MAX_COMMENTS},{MAX_COMMENTS},0,0",
        *cookies,
        watch_url(video_id),
    ])
    blocked = "not a bot" in (result.stderr or "")
    if result.returncode != 0 or not result.stdout.strip():
        return [], blocked
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], blocked
    return list(data.get("comments") or []), blocked


def links_in_comment(text: str) -> list[tuple[str, str]]:
    """(label, url) pairs of one comment, in the order they were written.

    A lecturer writes "Презентация: <link>" line by line, so the label is
    what stands before the colon. Links without a label of their own keep
    an empty one rather than borrowing the label above them.
    """
    labelled = {
        trimmed(match.group("url")): match.group("label").strip()
        for match in LABELLED_LINK.finditer(text)
    }
    return [
        (labelled.get(trimmed(url), ""), trimmed(url))
        for url in ANY_LINK.findall(text)
    ]


def trimmed(url: str) -> str:
    """A link written inside a sentence collects its punctuation."""
    return url.rstrip(").,;>\"'")


def is_document_link(url: str) -> bool:
    return bool(
        DRIVE_FILE.search(url)
        or DRIVE_BY_ID.search(url)
        or DOCS_FILE.search(url)
    )


def find_presentation(comments: list[dict]) -> dict | None:
    """The presentation link among the comments, with what came with it.

    Only the uploader's own comments are trusted: a viewer's link under a
    lecture leads anywhere. Among them the label decides, and a lecturer
    who wrote no label at all still gets read - then the first document
    link of the comment is taken.
    """
    mine = [c for c in comments if c.get("author_is_uploader")]
    for comment in mine:
        pairs = links_in_comment(str(comment.get("text") or ""))
        documents = [
            (label, url) for label, url in pairs if is_document_link(url)
        ]
        if not documents:
            continue
        chosen = next(
            (
                (label, url)
                for label, url in documents
                if PRESENTATION_LABEL.search(label)
            ),
            documents[0],
        )
        return {
            "label": chosen[0],
            "url": chosen[1],
            "author": comment.get("author") or "",
            "other_materials": [
                {"label": label, "url": url}
                for label, url in documents
                if url != chosen[1]
            ],
        }
    return None


# --------------------------------------------------------------------------
# Downloading the file


def download_url(link: str) -> str:
    """The address that serves the file itself, not a page about it."""
    key = RESOURCE_KEY.search(link)
    drive = DRIVE_FILE.search(link) or DRIVE_BY_ID.search(link)
    if drive:
        query = {"id": drive.group(1), "export": "download"}
        if key:
            query["resourcekey"] = key.group(1)
        return (
            "https://drive.usercontent.google.com/download?"
            + urllib.parse.urlencode(query)
        )
    docs = DOCS_FILE.search(link)
    if docs:
        kind, file_id = docs.group(1), docs.group(2)
        return (
            f"https://docs.google.com/{kind}/d/{file_id}/"
            f"{DOCS_EXPORT.get(kind, 'export?format=pdf')}"
        )
    return link


def offered_name(response) -> str:
    """The file name the server offers, un-mangled.

    Google sends UTF-8 bytes in a latin-1 header, so a Russian name arrives
    as mojibake unless it is put back together.
    """
    disposition = response.headers.get("Content-Disposition") or ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition)
    if match:
        return urllib.parse.unquote(match.group(1))
    match = re.search(r'filename="([^"]*)"', disposition)
    if not match:
        return ""
    name = match.group(1)
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def suffix_for(name: str, content_type: str) -> str:
    if name and Path(name).suffix:
        return Path(name).suffix.lower()
    return CONTENT_TYPE_SUFFIX.get(content_type.split(";")[0].strip(), ".bin")


def confirm_url(html: str, url: str) -> str | None:
    """Google's "we cannot scan this file" page hides the real link in a
    form; without following it the download saves that page instead."""
    if "download-form" not in html and "confirm" not in html:
        return None
    action = re.search(r'action="([^"]+)"', html)
    fields = dict(
        re.findall(r'name="([^"]+)"\s+value="([^"]*)"', html)
    )
    if not fields:
        return None
    base = (action.group(1).replace("&amp;", "&") if action else url)
    joiner = "&" if "?" in base else "?"
    return base + joiner + urllib.parse.urlencode(fields)


def open_url(url: str):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": USER_AGENT}),
        timeout=REQUEST_TIMEOUT,
    )


def fetch_file(
    link: str, target_dir: Path, stem: str
) -> tuple[Path, str, int]:
    """Download the file; returns (saved path, offered name, bytes).

    Written to a temporary name and renamed at the end, so an interrupted
    download does not look like a finished one on the next run.
    """
    url = download_url(link)
    response = open_url(url)
    if (response.headers.get("Content-Type") or "").startswith("text/html"):
        with response:
            following = confirm_url(
                response.read().decode("utf-8", "replace"), url
            )
        if following is None:
            raise RuntimeError(
                "the link answered with a web page, not a file; it may need "
                "signing in to Google"
            )
        response = open_url(following)

    target_dir.mkdir(parents=True, exist_ok=True)
    temp = target_dir / f".{stem}.part"
    size = 0
    with response, temp.open("wb") as out:
        name = offered_name(response)
        content_type = response.headers.get("Content-Type") or ""
        while True:
            chunk = response.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)

    final = target_dir / f"{stem}{suffix_for(name, content_type)}"
    final.unlink(missing_ok=True)
    temp.replace(final)
    return final, name, size


# --------------------------------------------------------------------------
# One video


def presentations_dir(playlist_dir: Path) -> Path:
    return playlist_dir / PRESENTATIONS_DIRNAME


def existing_presentation(playlist_dir: Path, stem: str) -> Path | None:
    """The presentation already downloaded for this video, if any."""
    folder = presentations_dir(playlist_dir)
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob(f"{glob_escape(stem)}.*")):
        if path.suffix != SIDECAR_SUFFIX and path.is_file():
            return path
    return None


def glob_escape(text: str) -> str:
    """Video names hold brackets ([video id]), and glob reads those."""
    return re.sub(r"([\[\]])", r"[\1]", text)


def write_sidecar(
    playlist_dir: Path, stem: str, video_id: str, found: dict, saved: Path,
    offered: str, size: int,
) -> Path:
    path = presentations_dir(playlist_dir) / f"{stem}{SIDECAR_SUFFIX}"
    path.write_text(
        json.dumps(
            {
                "video": stem,
                "video_id": video_id,
                "file": saved.name,
                "original_name": offered,
                "bytes": size,
                "source": found["url"],
                "comment_label": found["label"],
                "comment_author": found["author"],
                "other_materials": found["other_materials"],
                "downloaded": datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit != "MB" else f"{size:.1f} MB"
        size /= 1024
    return f"{size:.1f} MB"


def offer_presentation(
    playlist_dir: Path,
    stem: str,
    *,
    cookies: list[str],
    auto_yes: bool,
    indent: str = "  ",
) -> tuple[Path | None, bool]:
    """Find and (after a y/n) download the presentation of one video.

    Returns (saved file or None, YouTube demanded a sign-in). Used both by
    this script and by the steps that chain it, so everything it says is
    indented to sit under the caller's line.
    """
    existing = existing_presentation(playlist_dir, stem)
    if existing is not None:
        print(
            f"{indent}Presentation already here: {existing.name}", flush=True
        )
        return existing, False

    video_id = video_id_from_stem(stem)
    if video_id is None:
        print(
            f"{indent}No [video id] in the file name, so the video page "
            "cannot be found; skipped.",
            flush=True,
        )
        return None, False

    print(f"{indent}Looking for a presentation under the video...", flush=True)
    comments, blocked = fetch_comments(video_id, cookies=cookies)
    if blocked:
        print(
            f"{indent}YouTube asked to confirm you are not a bot; pass "
            "cookies (--cookies-from-browser firefox) or try again later.",
            flush=True,
        )
        return None, True
    found = find_presentation(comments)
    if found is None:
        print(
            f"{indent}The author linked no presentation under this video "
            f"({len(comments)} comment(s) read).",
            flush=True,
        )
        return None, False

    label = found["label"] or "no label"
    print(f"{indent}Found ({label}): {found['url']}", flush=True)
    if not auto_yes:
        print(f"{indent}Download it? (y/n)", flush=True)
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print(f"{indent}Skipped: not confirmed.", flush=True)
            return None, False

    try:
        saved, offered, size = fetch_file(
            found["url"], presentations_dir(playlist_dir), stem
        )
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        print(f"{indent}WARNING: the download failed: {exc}", flush=True)
        return None, False
    write_sidecar(playlist_dir, stem, video_id, found, saved, offered, size)
    print(
        f"{indent}Saved: {PRESENTATIONS_DIRNAME}/{saved.name} "
        f"({human_size(size)}"
        + (f", offered as \"{offered}\"" if offered else "")
        + ")",
        flush=True,
    )
    if found["other_materials"]:
        print(
            f"{indent}The author linked {len(found['other_materials'])} more "
            f"file(s); they are listed in {stem}{SIDECAR_SUFFIX}, not "
            "downloaded.",
            flush=True,
        )
    return saved, False


# --------------------------------------------------------------------------
# CLI


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download the presentation the lecturer linked in a comment "
            "under the video (free, no API key)."
        )
    )
    parser.add_argument(
        "channel_folder",
        help=(
            "Channel ref under _channels/ "
            "(e.g. Game_Design\\_makingitright9305)"
        ),
    )
    parser.add_argument(
        "playlist_folder",
        help="Playlist folder name under <channel>/_playlists",
    )
    parser.add_argument(
        "--next",
        dest="next_count",
        default="1",
        metavar="N",
        help=(
            "How many videos to look at this session (default: 1); 'all' "
            "takes every video without a presentation yet"
        ),
    )
    parser.add_argument(
        "--video",
        metavar="STEM",
        help="Only the video with this file name (without extension)",
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
        "--yes",
        action="store_true",
        help="Download every presentation found without asking",
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
    playlist_dir = channel_dir / "_playlists" / args.playlist_folder
    if not playlist_dir.is_dir():
        raise SystemExit(f"Playlist folder not found: {playlist_dir}")

    videos = list_videos(playlist_dir)
    if not videos:
        raise SystemExit(f"No video/audio files found in {playlist_dir}")
    if args.video and not any(video.stem == args.video for video in videos):
        raise SystemExit(f"No video named '{args.video}' in {playlist_dir}")
    wanted = [
        video for video in videos if not args.video or video.stem == args.video
    ]
    pending = [
        video
        for video in wanted
        if existing_presentation(playlist_dir, video.stem) is None
    ]
    session = (
        pending if args.next_count is None else pending[: args.next_count]
    )

    print(f"Playlist folder: {playlist_dir}", flush=True)
    print(
        f"Videos: {len(wanted)} total, {len(wanted) - len(pending)} with a "
        f"presentation already, {len(session)} in this session "
        f"(--next {next_label(args.next_count)}).",
        flush=True,
    )
    if not session:
        print(
            "Nothing to do: every video already has its presentation.",
            flush=True,
        )
        return 0

    cookies = cookie_args(args.cookies, args.cookies_from_browser)
    saved_count = 0
    for position, video in enumerate(session, start=1):
        print(f"[{position}/{len(session)}] {video.name}", flush=True)
        saved, blocked = offer_presentation(
            playlist_dir, video.stem, cookies=cookies, auto_yes=args.yes
        )
        if saved is not None:
            saved_count += 1
        if blocked:
            print(
                "Session stopped: the block is per IP or per account, and "
                "the remaining videos would only repeat it.",
                flush=True,
            )
            break

    print(f"Session done: {saved_count} presentation(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
