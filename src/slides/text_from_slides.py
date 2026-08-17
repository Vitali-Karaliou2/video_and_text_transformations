#!/usr/bin/env python3
"""Extract text from slide images into slides.json (pipeline step 6).

Input: slide images produced by extract_slides.py in
_channels/<channel>/_playlists/<playlist>/SLIDES/<short-key>/, together with
scenes.csv. Output: slides.json in the same folder.

Two passes over the OpenAI API:

1. OCR pass - one vision request per slide image. The frame may show the
   slide inside application UI (PowerPoint, browser, Microsoft Teams with a
   participant panel); only the central shared-content rectangle is read.
   Within it, the slide title is separated from the regular text; photos
   (e.g. of the lecturer) are reported as such. Text is extracted verbatim,
   never translated.

2. Structure pass - one request over the whole slide sequence. It recognizes
   the "lecture within a lecture course" slide pattern: the info slide with
   date/lecturer (document metadata), foreword slides about the lecturer and
   the course, the lecture title slide (combined with the file-name number
   into the document title), the agenda slide whose bullets become the
   numbered table of contents, later slides matched to agenda items (the
   fuller slide title wins over a short agenda item), sections not announced
   in the agenda (inserted with a needs_confirmation flag) and closing
   slides. Section headings that must be invented or edited are only
   flagged - rewriting is left to the final-editing step of the pipeline.

The session start point is found by scanning SLIDES/: the first video whose
slide folder has images but no slides.json is processed first. --video picks
one video by name, which is how transcribe_videos.py --slides chains this
step.

Before reading a video's slides the estimated cost is printed and confirmed
(y/n), as in every other paid stage; --yes answers it. The prompt side of
the estimate is arithmetic - a "detail: high" frame costs what its size says
- and the replies go by the averages of a course.

Examples:
  python src/slides/text_from_slides.py _Autotesting lectures
  python src/slides/text_from_slides.py _Autotesting lectures --next 3
  python src/slides/text_from_slides.py _Autotesting lectures --video 01_Introduction
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from shared.api_key import read_api_key
from shared.media_files import list_videos
from shared.openai_chat import (
    MODEL,
    USD_PER_MTOKEN_COMPLETION,
    USD_PER_MTOKEN_PROMPT,
    chat_json,
)
from shared.project_paths import (
    WORKSPACE_ROOT,
    channels_dir,
    require_channel_ref,
)
from shared.transcripts import LANGUAGE_NAMES
from slides.extract_slides import SLIDES_DIRNAME, short_slide_keys, slides_out_dir

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

RESULT_FILENAME = "slides.json"
SLIDE_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
# What a "detail: high" image costs: the frame is scaled to fit a 2048 square,
# then its shortest side to 768, and every 512x512 tile of the result is
# charged (https://platform.openai.com/docs/guides/vision). A 1920x1080 screen
# share becomes 1366x768 - six tiles, 1105 tokens.
VISION_BOX = 2048
VISION_SHORT_SIDE = 768
VISION_TILE = 512
VISION_BASE_TOKENS = 85
VISION_TILE_TOKENS = 170
# A frame whose size the header does not give away (an exotic format): the
# estimate falls back to the usual screen share.
FALLBACK_FRAME = (1920, 1080)
# ~4 characters per token: the prompts and the replies here are English JSON.
CHARS_PER_TOKEN = 4.0
# What the model writes is not known before it writes it, so these are
# fitted to the nine lectures of the test-automation course (7 to 23 slides,
# token counts from the run logs): the replies come to ~650 + ~150 tokens per
# slide, and the structure request carries ~170 tokens of slide text. On the
# same nine the prompt estimate lands within a few per cent of the bill.
SLIDE_REPLY_TOKENS = 110
STRUCTURE_PROMPT_PER_SLIDE = 170
STRUCTURE_REPLY_TOKENS = 650
STRUCTURE_REPLY_PER_SLIDE = 40

OCR_SYSTEM_PROMPT = """\
You extract text from one frame of a screen-shared lecture video. The frame
may show the slide full-screen, or inside an application window (PowerPoint
editor, browser, Microsoft Teams) with toolbars, slide thumbnails, a
participant panel, chat, webcam tiles and other UI around the shared content.
Only the central shared-content rectangle - the slide itself - matters;
ignore every UI element around it (window title, menus, thumbnails,
participant names, webcam images, clock, taskbar).

Within the slide, separate the title from the regular text. The title is the
most prominent heading; large standalone text on the left of the slide also
counts as the title. Regular text is the bullet points / body lines in
reading order. Transcribe verbatim - do not fix, translate or paraphrase.

Reply with JSON only:
{
  "content": "text" | "photo" | "photo_and_text" | "empty",
  "title": string or null,
  "body": [one string per bullet or line of regular text],
  "photo_description": string or null (short, e.g. "photo of the lecturer"),
  "notes": [anything unusual worth flagging, may be empty]
}
"content" describes the slide itself: "photo" when the slide is essentially
a photograph, "photo_and_text" when a photo takes the place of the title or
accompanies the text, "empty" when the shared area is blank or shows no
meaningful slide."""

STRUCTURE_SYSTEM_PROMPT = """\
You reconstruct the structure of one lecture from the texts of its slides.
The lecture is one video from a playlist that corresponds to a lecture
course. Slides are given in order of appearance; for each slide you get its
extracted title, body text and scene timing.

Well-organized lecture courses follow a common slide pattern; recognize it
and use it:
- An info slide (often the very first) gives general facts about the
  recording: date, who recorded it. These go to document metadata, kept in
  English, and get no table-of-contents entry.
- Near the start of the first lecture of a course there may be foreword
  slides about the lecturer (often with a photo instead of a title) and
  about the course itself. They form unnumbered foreword sections placed
  before the numbered main sections.
- A slide containing only the lecture topic is the lecture title slide. The
  document title combines the leading number of the video file name with
  that topic (file "01_Introduction.mp4" + slide "Introduction to Test
  Automation" -> "01. Introduction to Test Automation").
- Soon after the lecture title slide an agenda slide usually follows
  ("Agenda" or a synonym): its bullet list is the table of contents of the
  whole lecture; a final "Q&A" item is a good marker that the list covers
  the entire main part. Number those items 1, 2, 3, ...
- Most later slides open the sections announced in the agenda: their titles
  match agenda items. When the slide title is a fuller wording of a short
  agenda item, prefer the slide title in the table of contents.
- A slide whose title matches no agenda item opens a section that was not
  announced: insert it into the table of contents at its actual position and
  flag it "needs_confirmation".
- Slides at the very end that get very little screen time (thanks, Q&A,
  closing photo) will likely be merged into one closing section at the
  final-editing step; give them role "closing" and no separate numbered
  sections.

Slide text may contain imperfect English; never rewrite it, but when a slide
title used as a section heading is badly worded, add the flag
"title_needs_editing". When a section heading has to be invented (a photo
instead of a title), propose a title and add the flag "title_invented".
Do not translate any extracted text.

Reply with JSON only:
{
  "document": {
    "title": string,
    "meta": {"date": string|null, "recorded_by": string|null,
             "language": string|null},
    "toc": [
      {"number": string|null, "title": string, "slide": string|null,
       "source": "agenda" | "slide_title" | "inserted" | "invented",
       "flags": [strings], "note": string|null}
    ]
  },
  "slides": [
    {"file": string,
     "role": "lecture_info" | "foreword" | "lecture_title" | "agenda" |
             "section" | "content" | "closing" | "photo",
     "section": string|null (title of the toc entry the slide belongs to),
     "flags": [strings], "note": string|null}
  ]
}
List every input slide exactly once in "slides", in the input order."""


def frame_size(image: Path) -> tuple[int, int] | None:
    """(width, height) read from the file header, without an image library.

    Only what extract_slides.py writes is understood - PNG, JPEG, WebP.
    None when the file cannot be read or says nothing: this serves a cost
    estimate, and an estimate must not be the thing that stops a run.
    """
    try:
        data = image.read_bytes()
    except OSError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            # The frame headers (SOF0..SOF15) carry the size; the rest of the
            # markers only say how far to jump.
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return (
                    int.from_bytes(data[offset + 7:offset + 9], "big"),
                    int.from_bytes(data[offset + 5:offset + 7], "big"),
                )
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            offset += 2 + int.from_bytes(data[offset + 2:offset + 4], "big")
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X":
            return (
                int.from_bytes(data[24:27], "little") + 1,
                int.from_bytes(data[27:30], "little") + 1,
            )
        if data[12:16] == b"VP8 ":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
    return None


def vision_tokens(width: int, height: int) -> int:
    """What one "detail: high" frame of this size costs in prompt tokens."""
    if max(width, height) > VISION_BOX:
        scale = VISION_BOX / max(width, height)
        width, height = round(width * scale), round(height * scale)
    if min(width, height) > VISION_SHORT_SIDE:
        scale = VISION_SHORT_SIDE / min(width, height)
        width, height = round(width * scale), round(height * scale)
    tiles = -(-width // VISION_TILE) * -(-height // VISION_TILE)
    return VISION_BASE_TOKENS + VISION_TILE_TOKENS * tiles


def estimate_cost(images: list[Path]) -> tuple[int, int, float]:
    """(prompt tokens, completion tokens, dollars) for one video.

    The OCR pass is arithmetic - the frames are all the same size and their
    price is fixed by it. What the slides say is not known before they are
    read, so the structure pass goes by the averages of the course.
    """
    size = frame_size(images[0]) if images else None
    per_image = vision_tokens(*(size or FALLBACK_FRAME))
    ocr_prompt = round(len(OCR_SYSTEM_PROMPT) / CHARS_PER_TOKEN)
    prompt = len(images) * (per_image + ocr_prompt)
    completion = len(images) * SLIDE_REPLY_TOKENS
    prompt += round(len(STRUCTURE_SYSTEM_PROMPT) / CHARS_PER_TOKEN)
    prompt += len(images) * STRUCTURE_PROMPT_PER_SLIDE
    completion += STRUCTURE_REPLY_TOKENS
    completion += len(images) * STRUCTURE_REPLY_PER_SLIDE
    cost = (
        prompt * USD_PER_MTOKEN_PROMPT + completion * USD_PER_MTOKEN_COMPLETION
    ) / 1_000_000
    return prompt, completion, cost


def image_data_url(path: Path) -> str:
    mime = MIME_TYPES[path.suffix.lower()]
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def ocr_slide(api_key: str, image: Path, usage: dict[str, int]) -> dict:
    result = chat_json(
        api_key,
        [
            {"role": "system", "content": OCR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Extract the slide text from this frame.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_data_url(image),
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
        usage,
    )
    return {
        "content": str(result.get("content", "text")),
        "title": result.get("title"),
        "body": [str(line) for line in result.get("body") or []],
        "photo_description": result.get("photo_description"),
        "notes": [str(note) for note in result.get("notes") or []],
    }


def read_scenes(csv_path: Path) -> dict[int, dict]:
    """Scene number -> timing info from scenes.csv (empty dict if absent)."""
    if not csv_path.is_file():
        return {}
    scenes: dict[int, dict] = {}
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    header_index = next(
        (i for i, line in enumerate(lines) if line.startswith("Scene Number")),
        None,
    )
    if header_index is None:
        return {}
    header = next(csv.reader([lines[header_index]]))
    for row in csv.reader(lines[header_index + 1 :]):
        if not row or not row[0].strip().isdigit():
            continue
        record = dict(zip(header, row))
        scenes[int(record["Scene Number"])] = {
            "number": int(record["Scene Number"]),
            "start_timecode": record.get("Start Timecode"),
            "end_timecode": record.get("End Timecode"),
            "start_seconds": float(record.get("Start Time (seconds)", 0) or 0),
            "end_seconds": float(record.get("End Time (seconds)", 0) or 0),
            "length_seconds": float(record.get("Length (seconds)", 0) or 0),
        }
    return scenes


def read_reveals(json_path: Path) -> dict[int, list[dict]]:
    """Scene number -> when the page gained content, from reveals.json.

    The slide image of a scene is its last state, so a page built line by
    line arrives here whole; these are the steps it took to get there, and
    they say when each part of it was named.
    """
    if not json_path.is_file():
        return {}
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        int(scene["scene"]): list(scene.get("reveals") or [])
        for scene in data.get("scenes") or []
        if str(scene.get("scene", "")).isdigit()
    }


def list_slide_images(folder: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file()
            and path.suffix.lower() in SLIDE_IMAGE_SUFFIXES
            and path.stem.lower().startswith("slide_")
        ),
        key=lambda path: path.name.lower(),
    )


def slide_scene_number(image: Path) -> int | None:
    digits = image.stem.split("_")[-1]
    return int(digits) if digits.isdigit() else None


def detect_original_language(playlist_dir: Path) -> str | None:
    """Language of the original transcription, from <playlist>/<XX>/ folders."""
    codes = sorted(
        entry.name
        for entry in playlist_dir.iterdir()
        if entry.is_dir()
        and len(entry.name) == 2
        and entry.name.isalpha()
        and entry.name.isupper()
        and entry.name != "EN"
    )
    if codes:
        return LANGUAGE_NAMES.get(codes[0], codes[0])
    if (playlist_dir / "EN").is_dir():
        return "English"
    return None


def analyze_structure(
    api_key: str,
    *,
    course: str,
    playlist: str,
    video: Path,
    video_position: tuple[int, int],
    original_language: str | None,
    slides: list[dict],
    usage: dict[str, int],
) -> dict:
    context = {
        "course": course,
        "playlist": playlist,
        "video_file": video.name,
        "video_position_in_playlist": (
            f"video {video_position[0]} of {video_position[1]}"
        ),
        "original_recording_language": original_language,
        "slides": [
            {
                "file": slide["file"],
                "scene": slide["scene"],
                "content": slide["content"],
                "title": slide["title"],
                "body": slide["body"],
                "photo_description": slide["photo_description"],
            }
            for slide in slides
        ],
    }
    return chat_json(
        api_key,
        [
            {"role": "system", "content": STRUCTURE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False, indent=2),
            },
        ],
        usage,
    )


def merge_structure(slides: list[dict], structure: dict) -> dict:
    """Fold the structure-pass verdicts into the per-slide records."""
    by_file = {
        str(item.get("file")): item for item in structure.get("slides") or []
    }
    for slide in slides:
        verdict = by_file.get(slide["file"], {})
        slide["role"] = verdict.get("role", "content")
        slide["section"] = verdict.get("section")
        slide["flags"] = [str(flag) for flag in verdict.get("flags") or []]
        slide["note"] = verdict.get("note")
    document = structure.get("document") or {}
    document.setdefault("title", "")
    document.setdefault("meta", {})
    document.setdefault("toc", [])
    return document


def process_video(
    video: Path,
    out_dir: Path,
    *,
    api_key: str,
    course: str,
    playlist: str,
    video_position: tuple[int, int],
    original_language: str | None,
) -> int:
    """Extract text from all slides of one video; return the slide count."""
    images = list_slide_images(out_dir)
    scenes = read_scenes(out_dir / "scenes.csv")
    reveals = read_reveals(out_dir / "reveals.json")
    usage: dict[str, int] = {}

    slides: list[dict] = []
    for index, image in enumerate(images, start=1):
        print(f"  [{index}/{len(images)}] {image.name}: extracting text...",
              flush=True)
        ocr = ocr_slide(api_key, image, usage)
        title = ocr["title"] or (
            f"({ocr['photo_description']})" if ocr["photo_description"] else ""
        )
        print(f"    -> {ocr['content']}: {title or '(no title)'}", flush=True)
        scene_number = slide_scene_number(image)
        slides.append(
            {
                "file": image.name,
                "scene": scenes.get(scene_number),
                "reveals": reveals.get(scene_number, []),
                "content": ocr["content"],
                "title": ocr["title"],
                "body": ocr["body"],
                "photo_description": ocr["photo_description"],
                "ocr_notes": ocr["notes"],
            }
        )

    print("  Analyzing slide sequence (document structure)...", flush=True)
    structure = analyze_structure(
        api_key,
        course=course,
        playlist=playlist,
        video=video,
        video_position=video_position,
        original_language=original_language,
        slides=slides,
        usage=usage,
    )
    document = merge_structure(slides, structure)

    result = {
        "video": video.name,
        "channel": f"_{course}",
        "playlist": playlist,
        "slides_folder": out_dir.name,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": MODEL,
        "document": document,
        "slides": slides,
    }
    result_path = out_dir / RESULT_FILENAME
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"  Tokens used: {usage.get('prompt_tokens', 0)} prompt + "
        f"{usage.get('completion_tokens', 0)} completion.",
        flush=True,
    )
    return len(slides)


def confirm_cost(images: list[Path], *, auto_yes: bool) -> bool:
    """Print what reading these slides will cost and ask, like every other
    paid stage of the pipeline does."""
    prompt, completion, cost = estimate_cost(images)
    print(
        f"  {len(images)} slide(s) -> ~{prompt} prompt + ~{completion} "
        f"completion tokens -> ~${cost:.2f} ({MODEL}).",
        flush=True,
    )
    if auto_yes:
        return True
    print("  Read the text of these slides? (y/n)", flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def has_slides(folder: Path) -> bool:
    return folder.is_dir() and bool(list_slide_images(folder))


def is_processed(folder: Path) -> bool:
    return (folder / RESULT_FILENAME).is_file()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract text from slide images into slides.json."
    )
    parser.add_argument(
        "channel_folder",
        help="Channel ref under _channels/ (e.g. _Autotesting or "
        "AI_for_Game_Design\\_BuildingAeon)",
    )
    parser.add_argument(
        "playlist_folder",
        help="Playlist folder name under <channel>/_playlists (e.g. lectures)",
    )
    parser.add_argument(
        "--next",
        dest="next_count",
        type=int,
        default=1,
        metavar="N",
        help="How many videos to process this session (default: 1)",
    )
    parser.add_argument(
        "--video",
        metavar="STEM",
        help=(
            "Only the video with this file name (without extension); used "
            "by transcribe_videos.py --slides to follow one video through "
            "the pipeline"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not ask for the cost confirmation before reading the slides",
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
    if args.next_count < 1:
        raise SystemExit("--next must be a positive number")

    try:
        channel_dir = require_channel_ref(
            channels_dir(args.workspace), args.channel_folder
        )
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    playlist_dir = channel_dir / "_playlists" / args.playlist_folder
    if not playlist_dir.is_dir():
        raise SystemExit(f"Playlist folder not found: {playlist_dir}")

    slides_dir = playlist_dir / SLIDES_DIRNAME
    videos = list_videos(playlist_dir)
    if not videos:
        raise SystemExit(f"No video/audio files found in {playlist_dir}")

    keys = short_slide_keys([video.stem for video in videos])
    course = channel_dir.name.lstrip("_")
    original_language = detect_original_language(playlist_dir)

    if args.video and not any(video.stem == args.video for video in videos):
        raise SystemExit(f"No video named '{args.video}' in {playlist_dir}")
    wanted = [
        video for video in videos if not args.video or video.stem == args.video
    ]
    eligible = [
        video
        for video in wanted
        if has_slides(slides_out_dir(slides_dir, video, keys))
    ]
    pending = [
        video
        for video in eligible
        if not is_processed(slides_out_dir(slides_dir, video, keys))
    ]
    session = pending[: args.next_count]

    print(f"Playlist folder: {playlist_dir}", flush=True)
    if args.video:
        if not eligible:
            state = "no slides extracted yet."
        elif not pending:
            state = "slides already read."
        else:
            folder = slides_out_dir(slides_dir, pending[0], keys)
            state = f"{len(list_slide_images(folder))} slide(s) to read."
        print(f"Video '{args.video}' of {len(videos)}: {state}", flush=True)
    else:
        print(
            f"Videos: {len(videos)} total, {len(eligible)} with slides, "
            f"{len(eligible) - len(pending)} already processed, "
            f"{len(session)} in this session (--next {args.next_count}).",
            flush=True,
        )
    if not session:
        print(
            "Nothing to do: "
            + (
                f"'{args.video}' has no slides to read."
                if args.video and not eligible
                else "slides.json exists for every video with slides."
            ),
            flush=True,
        )
        return 0

    # Asked for only now: a run with nothing to do (or with a mistyped
    # --video) has no business demanding an API key.
    api_key = read_api_key(args.workspace)

    processed = 0
    for index, video in enumerate(session, start=1):
        print(f"[{index}/{len(session)}] {video.name}", flush=True)
        out_dir = slides_out_dir(slides_dir, video, keys)
        if not confirm_cost(list_slide_images(out_dir), auto_yes=args.yes):
            print("  Skipped: the cost was not confirmed.", flush=True)
            continue
        processed += 1
        count = process_video(
            video,
            out_dir,
            api_key=api_key,
            course=course,
            playlist=args.playlist_folder,
            video_position=(videos.index(video) + 1, len(videos)),
            original_language=original_language,
        )
        print(
            f"  Saved: {out_dir.relative_to(playlist_dir) / RESULT_FILENAME} "
            f"({count} slide(s)).",
            flush=True,
        )

    print(f"Session done: {processed} of {len(session)} video(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
