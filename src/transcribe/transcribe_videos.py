#!/usr/bin/env python3
"""Transcribe downloaded videos with the OpenAI audio API (pipeline step 3).

Videos live in _channels/<channel>/_playlists/<playlist>/ and are processed
in file-name order. Results go to <playlist>/<LANG>/ (original language) and,
for non-English originals, to <playlist>/EN/ as well: <video stem>.txt and
<video stem>.srt per language, plus <video stem>.asr.json with the per-segment
recognition quality the API reports (used by check_transcripts.py).

If the playlist or the channel folder has a terms.txt glossary (or --terms
points at one), its terms are passed to the recognizer as its `prompt`, which
keeps the product and technology names of the course spelled correctly.

The paragraphs of the .txt follow the pauses of the recording, taken from
the audio with ffmpeg and cached beside the video (see silences.py); an .srt
carries no pauses of its own, and without the media file (remote mode) the
paragraphs fall back to being cut by length.

The session start point is found by scanning the result folders: the first
video without a complete result set is transcribed first. Press 'p' during a
session to stop after the current video; the next run resumes automatically.

Remote mode (--from-youtube / --url): videos are taken straight from the
channel playlist on YouTube (mapped to the local folder via
_cache/playlists.json) - the next untranscribed video in playlist order is
picked, only its audio is downloaded into a temp folder, and the video
metadata (title, date, duration, chapters/timecodes) is saved to
<playlist>/INFO/<stem>.json for the final-editing stage.

When the playlist folder is omitted in remote mode, the flat channel-wide
video list is used instead (_cache/videos.json built by the summary script,
newest first); each video's results go to its own playlist folder (or to
misc/ for videos outside any playlist).

--title-substr narrows the remote list to the videos whose title contains
the given substring; every match is offered in turn ('n' skips to the next
one), which pairs with --next all to walk a whole lecture series.

Pipeline flags: --orig-only skips the English pass; --edit chains
create_final_docs.py per video right after transcription; --slides runs
extract_slides.py and text_from_slides.py in between. The two are
independent: --slides alone stops with slides.json ready for a later
editing pass, --edit alone builds the document ToC from the video
timecodes, and together they walk the pipeline to the end. The slide
stages read the frames of the video file, so --slides is refused in remote
mode when no video is on disk - only the audio track is downloaded there.
With --edit the "next" video is the next one that is not fully *edited*
yet: an already transcribed but unedited video skips the transcription
stage and goes straight to editing. --annotate adds the 200-250 word
annotation .txt (see create_final_docs.py).

Examples:
  python src/transcribe/transcribe_videos.py _Autotesting lectures --lang ru
  python src/transcribe/transcribe_videos.py _Autotesting lectures --lang ru --next 3
  python src/transcribe/transcribe_videos.py _Autotesting lectures --slides --edit
  python src/transcribe/transcribe_videos.py _VladilenMinin start_it_karery_s_nulya_v_2025 \
      --lang ru --orig-only --from-youtube --edit
  python src/transcribe/transcribe_videos.py _AbbasGallyamov \
      --lang ru --orig-only --from-youtube --edit --annotate
  python src/transcribe/transcribe_videos.py AI_for_Game_Design\\_makingitright9305 \
      --lang ru --orig-only --from-youtube --edit --annotate \
      --title-substr "Lecture 1.1." --next all
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import lru_cache
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from channels.playlist_jobs import (  # noqa: E402
    duration_to_text,
    fetch_playlist_entries,
    fetch_video_info,
    load_channel_flat_jobs,
    load_playlist_meta,
    load_unlisted_playlist_entries,
    video_id_from_url,
    watch_url,
)
from shared.api_key import read_api_key  # noqa: E402
from shared.glossary import (  # noqa: E402
    GLOSSARY_FILENAME,
    WHISPER_PROMPT_TOKENS,
    find_glossary,
    load_terms,
    whisper_prompt,
)
from shared.media_files import (  # noqa: E402
    INFO_DIRNAME,
    list_videos,
    long_paths_enabled,
    local_media_by_id,
    remote_stem,
    stem_budget,
)
from shared.project_paths import (  # noqa: E402
    WORKSPACE_ROOT,
    channels_dir,
    require_channel_ref,
)
from shared.sessions import (  # noqa: E402
    PauseWatcher,
    next_label,
    normalize_next_count,
    run_tool,
)
from shared.settings_file import read_settings, reconcile_channel_ref  # noqa: E402
from shared.silences import SilenceIndex, load_silences  # noqa: E402
from shared.transcripts import (  # noqa: E402
    Segment,
    asr_meta_path,
    format_txt,
    normalize_lang,
    optional_float,
    segments_to_srt,
)
from shared.transcription_pricing import get_transcription_rate  # noqa: E402
from shared.yt_dlp_opts import youtube_media_args  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
TRANSLATIONS_URL = "https://api.openai.com/v1/audio/translations"
MODEL = "whisper-1"
# whisper-1 accepts <=25 MB per request; 24 min of 64 kbps mono mp3 is ~11.5 MB.
CHUNK_SECONDS = 1440
REQUEST_ATTEMPTS = 3
REQUEST_TIMEOUT = 600
PROGRESS_INTERVAL = 5.0


def result_files(stem: str, orig_dir: Path, en_dir: Path, needs_en: bool) -> list[Path]:
    files = [orig_dir / f"{stem}.txt", orig_dir / f"{stem}.srt"]
    if needs_en:
        files += [en_dir / f"{stem}.txt", en_dir / f"{stem}.srt"]
    return files


def is_transcribed(video: Path, orig_dir: Path, en_dir: Path, needs_en: bool) -> bool:
    return all(
        path.is_file() for path in result_files(video.stem, orig_dir, en_dir, needs_en)
    )


def select_session_videos(
    videos: list[Path], orig_dir: Path, en_dir: Path, needs_en: bool, count: int
) -> list[Path]:
    pending = [
        video
        for video in videos
        if not is_transcribed(video, orig_dir, en_dir, needs_en)
    ]
    return pending[:count]


def probe_duration(path: Path, ffprobe: str) -> float:
    result = run_tool(
        [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path.name}:\n{result.stderr}")
    value = result.stdout.strip()
    try:
        return float(value)
    except ValueError:
        raise RuntimeError(f"ffprobe returned no duration for {path.name}: {value!r}")


def extract_audio_chunks(
    video: Path, tmp_dir: Path, ffmpeg: str, ffprobe: str
) -> list[tuple[Path, float]]:
    """Extract 16 kHz mono mp3 chunks (with durations) that fit the whisper-1
    request limit. The segment muxer can emit a degenerate near-empty trailing
    chunk (duration N/A); such chunks are dropped."""
    pattern = tmp_dir / "chunk_%03d.mp3"
    result = run_tool(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i", str(video),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "64k",
            "-f", "segment",
            "-segment_time", str(CHUNK_SECONDS),
            "-y",
            str(pattern),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {video.name}:\n{result.stderr}")
    chunks: list[tuple[Path, float]] = []
    for path in sorted(tmp_dir.glob("chunk_*.mp3")):
        try:
            seconds = probe_duration(path, ffprobe)
        except RuntimeError:
            seconds = 0.0
        if seconds > 0.1:
            chunks.append((path, seconds))
    if not chunks:
        raise RuntimeError(f"ffmpeg produced no audio chunks for {video.name}")
    return chunks


def multipart_body(
    fields: dict[str, str], file_name: str, file_bytes: bytes
) -> tuple[bytes, str]:
    boundary = f"----yt-dlp-transcribe-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            f"Content-Type: audio/mpeg\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), boundary


def call_audio_api(
    url: str, api_key: str, fields: dict[str, str], chunk: Path
) -> dict:
    body, boundary = multipart_body(fields, chunk.name, chunk.read_bytes())
    last_error: Exception | None = None
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code in (429, 500, 502, 503, 504) and attempt < REQUEST_ATTEMPTS:
                last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
                time.sleep(10 * attempt)
                continue
            raise SystemExit(f"OpenAI API error (HTTP {exc.code}):\n{detail}")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < REQUEST_ATTEMPTS:
                time.sleep(10 * attempt)
                continue
    raise SystemExit(f"OpenAI API request failed after retries: {last_error}")


class ProgressCalibration:
    """Estimate chunk processing time from chunks already completed.

    The API gives no real progress, so the percentage shown is elapsed time
    vs. an expected total. The first chunk uses a static guess for whisper-1
    speed; every completed chunk records its actual processing-time-to-audio
    ratio, and later chunks (including the EN pass and further videos in the
    session) use the average of the observed ratios.
    """

    def __init__(self) -> None:
        self.ratios: list[float] = []

    def expected_seconds(self, chunk_seconds: float) -> float:
        if self.ratios:
            avg_ratio = sum(self.ratios) / len(self.ratios)
            return max(5.0, avg_ratio * chunk_seconds)
        return 15.0 + 0.15 * chunk_seconds

    def record(self, chunk_seconds: float, elapsed: float) -> None:
        if chunk_seconds > 0 and elapsed > 0:
            self.ratios.append(elapsed / chunk_seconds)


def call_audio_api_with_progress(
    url: str,
    api_key: str,
    fields: dict[str, str],
    chunk: Path,
    *,
    prefix: str,
    chunk_seconds: float,
    calibration: ProgressCalibration,
) -> dict:
    """Run the blocking API call in a worker; report progress every 5 seconds
    (estimated percentage, capped at 99% until the response arrives)."""
    expected = calibration.expected_seconds(chunk_seconds)
    interactive = sys.stdout.isatty()
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call_audio_api, url, api_key, fields, chunk)
        while True:
            try:
                data = future.result(timeout=PROGRESS_INTERVAL)
                break
            except FutureTimeoutError:
                elapsed = time.monotonic() - start
                percent = min(99, int(elapsed / expected * 100))
                if interactive:
                    print(f"\r{prefix}  {percent}%", end="", flush=True)
                else:
                    print(f"{prefix}  {percent}%", flush=True)
    calibration.record(chunk_seconds, time.monotonic() - start)
    if interactive:
        print(f"\r{prefix}  100%", flush=True)
    else:
        print(f"{prefix}  100%", flush=True)
    return data


@lru_cache(maxsize=None)
def glossary_prompt(
    playlist_dir: Path, channel_dir: Path, explicit: Path | None
) -> str:
    """The course terms handed to the recognizer as its `prompt`.

    Priming whisper with the names it is about to hear is what keeps
    "Playwright" from becoming "PlevRite", and it costs nothing. Cached (and
    reported) once per folder, since a session transcribes many videos.
    """
    path = find_glossary(playlist_dir, channel_dir, explicit)
    terms = load_terms(path)
    prompt = whisper_prompt(terms)
    if path is not None:
        kept = prompt.count(",") + 1 if prompt else 0
        dropped = (
            f", {len(terms) - kept} did not fit the {WHISPER_PROMPT_TOKENS}-token "
            "prompt limit" if kept < len(terms) else ""
        )
        print(f"Glossary: {path} ({kept} term(s){dropped}).", flush=True)
    return prompt


def transcribe_chunks(
    chunks: list[tuple[Path, float]],
    api_key: str,
    *,
    url: str,
    fields: dict[str, str],
    label: str,
    calibration: ProgressCalibration,
) -> tuple[str, list[Segment]]:
    texts: list[str] = []
    segments: list[Segment] = []
    offset = 0.0
    for index, (chunk, chunk_seconds) in enumerate(chunks, start=1):
        data = call_audio_api_with_progress(
            url,
            api_key,
            fields,
            chunk,
            prefix=f"    {label}: chunk {index}/{len(chunks)}",
            chunk_seconds=chunk_seconds,
            calibration=calibration,
        )
        texts.append(str(data.get("text", "")).strip())
        for seg in data.get("segments", []):
            segments.append(
                Segment(
                    start=offset + float(seg["start"]),
                    end=offset + float(seg["end"]),
                    text=str(seg["text"]).strip(),
                    avg_logprob=optional_float(seg.get("avg_logprob")),
                    no_speech_prob=optional_float(seg.get("no_speech_prob")),
                    compression_ratio=optional_float(seg.get("compression_ratio")),
                )
            )
        offset += chunk_seconds
    return "\n".join(text for text in texts if text), segments


def write_asr_meta(
    out_dir: Path, stem: str, segments: list[Segment], *, language: str
) -> Path | None:
    """Per-segment recognition quality, kept for the offline checks.

    The API reports how confident it was (avg_logprob) and how likely the
    segment is not speech at all; check_transcripts.py uses that to tell a
    rare word from a misheard one. Written as a sidecar, not as one of the
    result_files(), so transcripts made before this existed stay complete.
    """
    if not any(seg.avg_logprob is not None for seg in segments):
        return None
    path = asr_meta_path(out_dir, stem)
    payload = {
        "model": MODEL,
        "language": language,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "segments": [
            {
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text,
                "avg_logprob": seg.avg_logprob,
                "no_speech_prob": seg.no_speech_prob,
                "compression_ratio": seg.compression_ratio,
            }
            for seg in segments
        ],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return path


def write_results(
    out_dir: Path,
    stem: str,
    text: str,
    segments: list[Segment],
    *,
    language: str,
    pauses: SilenceIndex | None = None,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / f"{stem}.txt"
    srt_path = out_dir / f"{stem}.srt"
    txt_path.write_text(
        format_txt(text, segments, pauses=pauses) + "\n", encoding="utf-8"
    )
    srt_path.write_text(segments_to_srt(segments), encoding="utf-8")
    written = [txt_path, srt_path]
    meta_path = write_asr_meta(out_dir, stem, segments, language=language)
    if meta_path is not None:
        written.append(meta_path)
    return written


def confirm_video(
    video: Path, ffprobe: str, usd_per_minute: float, passes: int, *, auto_yes: bool
) -> bool:
    minutes = probe_duration(video, ffprobe) / 60.0
    cost = minutes * usd_per_minute * passes
    pass_note = (
        "2 passes: original + English" if passes == 2 else "1 pass: English only"
    )
    print(
        f"  Duration {minutes:.1f} min -> estimated cost ${cost:.2f} ({pass_note}).",
        flush=True,
    )
    if auto_yes:
        return True
    print("  Transcribe this video? (y/n)", flush=True)
    try:
        answer = input().strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def transcribe_video(
    video: Path,
    *,
    api_key: str,
    lang: str,
    needs_en: bool,
    orig_dir: Path,
    en_dir: Path,
    ffmpeg: str,
    ffprobe: str,
    calibration: ProgressCalibration,
    stem: str | None = None,
    prompt: str = "",
) -> float:
    """Transcribe one video (original language + EN pass); return audio minutes.

    `stem` overrides the result file stem (remote mode: the media is a
    temporary audio download whose own name is meaningless). `prompt` is the
    course glossary handed to the recognizer (see glossary.py).
    """
    stem = stem or video.stem
    tmp_dir = Path(tempfile.mkdtemp(prefix="transcribe_"))
    try:
        print(f"  Extracting audio ({video.name})...", flush=True)
        chunks = extract_audio_chunks(video, tmp_dir, ffmpeg, ffprobe)
        minutes = sum(seconds for _, seconds in chunks) / 60.0
        print(f"  Audio: {minutes:.1f} min, {len(chunks)} chunk(s).", flush=True)

        playlist_dir = orig_dir.parent
        # The paragraphs of the .txt follow the pauses of the recording;
        # in remote mode the media is a throwaway download, so there is
        # nothing worth listening to (and nowhere to keep the sidecar).
        pauses = (
            SilenceIndex(load_silences(video, ffmpeg=ffmpeg))
            if stem == video.stem
            else None
        )

        fields = {
            "model": MODEL,
            "language": lang,
            "response_format": "verbose_json",
        }
        if prompt:
            fields["prompt"] = prompt
        text, segments = transcribe_chunks(
            chunks,
            api_key,
            url=TRANSCRIPTIONS_URL,
            fields=fields,
            label=lang.upper(),
            calibration=calibration,
        )
        for path in write_results(
            orig_dir, stem, text, segments, language=lang, pauses=pauses
        ):
            print(f"  Saved: {path.relative_to(playlist_dir)}", flush=True)

        if needs_en:
            fields_en = {"model": MODEL, "response_format": "verbose_json"}
            if prompt:
                fields_en["prompt"] = prompt
            text_en, segments_en = transcribe_chunks(
                chunks,
                api_key,
                url=TRANSLATIONS_URL,
                fields=fields_en,
                label="EN",
                calibration=calibration,
            )
            for path in write_results(
                en_dir, stem, text_en, segments_en, language="en", pauses=pauses
            ):
                print(f"  Saved: {path.relative_to(playlist_dir)}", flush=True)

        return minutes
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Remote mode: transcribe straight from the YouTube playlist (no local video)


def remote_transcribed(
    video_id: str, orig_dir: Path, en_dir: Path, needs_en: bool
) -> bool:
    """A result .srt with the [video id] marker exists in every needed folder."""
    marker = f"[{video_id}]"
    for folder in [orig_dir] + ([en_dir] if needs_en else []):
        if not folder.is_dir() or not any(
            marker in path.stem for path in folder.glob("*.srt")
        ):
            return False
    return True


def write_video_info(
    playlist_dir: Path, stem: str, info: dict, playlist_meta: dict, index: int
) -> Path:
    chapters = [
        {
            "start": float(chapter.get("start_time") or 0.0),
            "end": (
                float(chapter["end_time"])
                if chapter.get("end_time") is not None
                else None
            ),
            "title": str(chapter.get("title") or "").strip(),
        }
        for chapter in info.get("chapters") or []
    ]
    upload = str(info.get("upload_date") or "")
    payload = {
        "id": info.get("id"),
        "url": info.get("webpage_url") or watch_url(str(info.get("id"))),
        "title": info.get("title"),
        "channel": (
            playlist_meta.get("channel_name")
            or info.get("channel")
            or info.get("uploader")
        ),
        "playlist": playlist_meta.get("title"),
        "playlist_index": index,
        "date": f"{upload[:4]}-{upload[4:6]}" if len(upload) == 8 else "",
        "duration_seconds": info.get("duration"),
        "duration_text": duration_to_text(info.get("duration")),
        "chapters": chapters,
    }
    out_dir = playlist_dir / INFO_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def download_audio(
    url: str,
    tmp_dir: Path,
    *,
    cookies: Path | None = None,
    cookies_from_browser: str | None = None,
) -> Path:
    """Download only the audio track of the video into tmp_dir."""
    result = run_tool(
        [
            sys.executable, "-m", "yt_dlp",
            "-f", "bestaudio/best",
            "--no-playlist",
            *youtube_media_args(cookies, cookies_from_browser),
            "-o", str(tmp_dir / "audio.%(ext)s"),
            url,
        ]
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        hint = ""
        if "403" in err or "Forbidden" in err:
            hint = (
                "\nYouTube refused the media URL. Retry with "
                "--cookies-from-browser firefox (browser closed), "
                "or --cookies path\\to\\cookies.txt."
            )
        raise SystemExit(f"Audio download failed:\n{err}{hint}")
    for path in tmp_dir.iterdir():
        if path.is_file() and path.stem == "audio":
            return path
    raise SystemExit("Audio download produced no file")


def find_transcribed_stem(video_id: str, orig_dir: Path) -> str | None:
    """Stem of an existing transcript with the [video id] marker, if any."""
    marker = f"[{video_id}]"
    if orig_dir.is_dir():
        for path in sorted(orig_dir.glob("*.srt")):
            if marker in path.stem:
                return path.stem
    return None


def remote_edited(
    playlist_dir: Path, stem: str, *, lang: str, needs_en: bool, annotate: bool
) -> bool:
    """Final documents (per create_final_docs rules) exist for this stem."""
    from docs import create_final_docs as cfd
    orig_code = lang.upper()
    lang_codes = [orig_code] + (["EN"] if needs_en else [])
    return cfd.is_processed(
        playlist_dir, stem, lang_codes, annotate=annotate, orig_code=orig_code
    )


def remote_session(
    args: argparse.Namespace,
    *,
    lang: str,
    needs_en: bool,
    channel_dir: Path,
    playlist_dir: Path | None,
    api_key: str,
    usd_per_minute: float,
) -> tuple[int, list[tuple[str, str]]]:
    """Transcribe the next video(s) straight from YouTube.

    playlist_dir=None means the flat channel-wide mode. Returns the number
    of videos transcribed and the list of (playlist folder, stem) pairs to
    chain into the slide stages (--slides) and the final editing (--edit);
    with --edit the list also holds the videos taken straight to editing.
    """
    if playlist_dir is None:
        jobs, channel_name = load_channel_flat_jobs(channel_dir)
        print(
            f'Channel: "{channel_name}" - flat video list '
            f"({len(jobs)} videos, newest first).",
            flush=True,
        )
        scope_note = "in the channel"
    else:
        playlist_meta = load_playlist_meta(channel_dir, args.playlist_folder)
        print(
            f'YouTube playlist: "{playlist_meta["title"]}" '
            f'({playlist_meta["id"]})',
            flush=True,
        )
        playlist_id = str(playlist_meta["id"] or "")
        if playlist_id.startswith("unlisted:"):
            print("Loading the hand-written unlisted series...", flush=True)
            raw_entries = load_unlisted_playlist_entries(
                channel_dir, args.playlist_folder
            )
        else:
            print("Fetching the playlist entry list...", flush=True)
            raw_entries = fetch_playlist_entries(
                playlist_id,
                cookies=args.cookies,
                cookies_from_browser=args.cookies_from_browser,
            )
        jobs = [
            {**entry, "folder": args.playlist_folder,
             "playlist_meta": playlist_meta}
            for entry in raw_entries
        ]
        scope_note = "in the playlist"

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
        if not jobs:
            print("Nothing to do: no video title contains the substring.", flush=True)
            return 0, []

    if args.url:
        wanted = video_id_from_url(args.url)
        matched = [job for job in jobs if job["id"] == wanted]
        if matched:
            jobs = matched
        elif playlist_dir is not None:
            jobs = [
                {"id": wanted, "title": "", "duration": None, "index": 0,
                 "url": watch_url(wanted), "folder": args.playlist_folder,
                 "playlist_meta": playlist_meta}
            ]
        else:
            raise SystemExit(
                f"--url video {wanted} is not in the channel video cache; "
                "refresh the summary or pass the playlist folder explicitly."
            )

    def job_dirs(job: dict) -> tuple[Path, Path, Path]:
        pdir = channel_dir / "_playlists" / job["folder"]
        return pdir, pdir / lang.upper(), pdir / "EN"

    pending: list[dict] = []
    transcribed_count = 0
    for job in jobs:
        pdir, orig_dir, en_dir = job_dirs(job)
        job["transcribed"] = remote_transcribed(
            job["id"], orig_dir, en_dir, needs_en
        )
        if job["transcribed"]:
            transcribed_count += 1
            if not args.edit:
                continue
            stem = find_transcribed_stem(job["id"], orig_dir)
            if stem and remote_edited(
                pdir, stem, lang=lang, needs_en=needs_en,
                annotate=args.annotate,
            ):
                continue
            job["stem"] = stem
        pending.append(job)

    session = pending[: args.next_count]
    edited_note = (
        f", {transcribed_count - sum(1 for j in pending if j['transcribed'])}"
        " fully edited" if args.edit else ""
    )
    print(
        f"Videos: {len(jobs)} {scope_note}, "
        f"{transcribed_count} already transcribed{edited_note}, "
        f"{len(session)} in this session (--next {next_label(args.next_count)}).",
        flush=True,
    )
    if not session:
        print(
            "Nothing to do: every video is "
            + ("edited." if args.edit else "transcribed."),
            flush=True,
        )
        return 0, []
    print("Press 'p' to stop after the current video.", flush=True)

    local_media_cache: dict[str, dict[str, Path]] = {}
    watcher = PauseWatcher()
    calibration = ProgressCalibration()
    passes = 2 if needs_en else 1
    total_minutes = 0.0
    processed = 0
    edit_jobs: list[tuple[str, str]] = []
    for position, entry in enumerate(session, start=1):
        pdir, orig_dir, en_dir = job_dirs(entry)
        print(
            f"[{position}/{len(session)}] "
            f"{entry['title'] or entry['id']}"
            + (f"  [{entry['folder']}]" if playlist_dir is None else ""),
            flush=True,
        )

        if entry["transcribed"]:
            stem = entry.get("stem")
            if not stem:
                print(
                    "  WARNING: transcribed, but the transcript stem was "
                    "not found; skipping.",
                    flush=True,
                )
                continue
            print(
                "  Already transcribed - queueing for final editing only.",
                flush=True,
            )
            if not (pdir / INFO_DIRNAME / f"{stem}.json").is_file():
                print("  Fetching video metadata (INFO was missing)...",
                      flush=True)
                info = fetch_video_info(
                    entry["url"],
                    cookies=args.cookies,
                    cookies_from_browser=args.cookies_from_browser,
                )
                info_path = write_video_info(
                    pdir, stem, info, entry["playlist_meta"], entry["index"]
                )
                print(f"  Saved: {info_path.relative_to(pdir)}", flush=True)
            edit_jobs.append((entry["folder"], stem))
            continue

        print("  Fetching video metadata...", flush=True)
        info = fetch_video_info(
            entry["url"],
            cookies=args.cookies,
            cookies_from_browser=args.cookies_from_browser,
        )
        title = str(info.get("title") or entry["title"] or entry["id"])
        seconds = float(info.get("duration") or entry.get("duration") or 0.0)
        if entry["folder"] not in local_media_cache:
            created = not pdir.is_dir()
            pdir.mkdir(parents=True, exist_ok=True)
            if created:
                print(
                    f"  Created playlist folder: "
                    f"{pdir.relative_to(channel_dir)}",
                    flush=True,
                )
            local_media_cache[entry["folder"]] = local_media_by_id(pdir)
        local = local_media_cache[entry["folder"]].get(entry["id"])
        budget = stem_budget(pdir)
        stem = (
            local.stem if local is not None
            else remote_stem(entry["index"], title, entry["id"], max_len=budget)
        )
        if len(stem) > budget:
            advice = (
                "move the channel closer to the drive root"
                if long_paths_enabled()
                else "move the channel closer to the drive root or enable "
                "long paths in Windows"
            )
            print(
                f"  WARNING: the file name is {len(stem) - budget} character(s) "
                f"longer than the path limit allows here; {advice}.",
                flush=True,
            )
        elif local is None and len(stem) < len(
            remote_stem(entry["index"], title, entry["id"])
        ):
            print(
                f"  Long path: the file name was shortened to {len(stem)} "
                "characters.",
                flush=True,
            )

        minutes = seconds / 60.0
        cost = minutes * usd_per_minute * passes
        pass_note = (
            "2 passes: original + English" if passes == 2
            else f"1 pass: {lang.upper()} only" if lang != "en"
            else "1 pass: English only"
        )
        print(
            f"  Duration {minutes:.1f} min -> estimated cost ${cost:.2f} "
            f"({pass_note}).",
            flush=True,
        )
        if not args.yes:
            print("  Transcribe this video? (y/n)", flush=True)
            try:
                answer = input().strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                if args.title_substr:
                    print("  Skipped; moving on to the next match.", flush=True)
                    continue
                print("Session stopped: transcription not confirmed.",
                      flush=True)
                break

        info_path = write_video_info(
            pdir, stem, info, entry["playlist_meta"], entry["index"]
        )
        print(f"  Saved: {info_path.relative_to(pdir)}", flush=True)

        tmp_dir: Path | None = None
        try:
            if local is not None:
                print(f"  Using the local file: {local.name}", flush=True)
                source = local
            else:
                tmp_dir = Path(tempfile.mkdtemp(prefix="yt_audio_"))
                print("  Downloading the audio track...", flush=True)
                source = download_audio(
                    entry["url"],
                    tmp_dir,
                    cookies=args.cookies,
                    cookies_from_browser=args.cookies_from_browser,
                )
            total_minutes += transcribe_video(
                source,
                api_key=api_key,
                lang=lang,
                needs_en=needs_en,
                orig_dir=orig_dir,
                en_dir=en_dir,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
                calibration=calibration,
                stem=stem,
                prompt=glossary_prompt(pdir, channel_dir, args.terms),
            )
        finally:
            if tmp_dir is not None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        processed += 1
        edit_jobs.append((entry["folder"], stem))
        if watcher.pause_requested() and position < len(session):
            print("Pause requested: stopping after the current video.",
                  flush=True)
            break

    print(
        f"Session done: {processed} video(s) transcribed, "
        f"{total_minutes:.1f} audio minutes, estimated cost "
        f"${total_minutes * usd_per_minute * passes:.2f}.",
        flush=True,
    )
    return processed, edit_jobs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe downloaded videos via the OpenAI audio API."
    )
    parser.add_argument(
        "channel_folder",
        nargs="?",
        default=None,
        help="Channel ref under _channels/ (e.g. _Autotesting or "
        "AI_for_Game_Design\\_BuildingAeon); may come from --settings "
        "instead, and overrides it when both are given",
    )
    parser.add_argument(
        "playlist_folder",
        nargs="?",
        default=None,
        help=(
            "Playlist folder name under <channel>/_playlists (e.g. "
            "lectures). May be omitted in remote mode (--from-youtube / "
            "--url): the flat channel-wide video list is used then. May "
            "also come from --settings"
        ),
    )
    parser.add_argument(
        "--lang",
        default="ru",
        metavar="XX",
        help=(
            "Original language, two-letter code (default: ru). Non-English "
            "originals also get an English pass into the EN folder"
        ),
    )
    parser.add_argument(
        "--next",
        dest="next_count",
        default="1",
        metavar="N",
        help=(
            "How many videos to transcribe this session (default: 1); "
            "'all' takes every pending video"
        ),
    )
    parser.add_argument(
        "--title-substr",
        dest="title_substr",
        default=None,
        metavar="TEXT",
        help=(
            "Remote mode only: keep just the videos whose title contains "
            "this substring (case-insensitive). Every match is offered in "
            "turn, and answering 'n' skips to the next match instead of "
            "ending the session"
        ),
    )
    parser.add_argument(
        "--orig-only",
        action="store_true",
        help=(
            "Transcribe only the original language (skip the English pass "
            "for non-English originals)"
        ),
    )
    parser.add_argument(
        "--from-youtube",
        action="store_true",
        help=(
            "Take the next untranscribed video straight from the YouTube "
            "playlist (audio only is downloaded to a temp folder; metadata "
            "with timecodes is saved to INFO/)"
        ),
    )
    parser.add_argument(
        "--url",
        default=None,
        metavar="URL",
        help=(
            "Transcribe this specific YouTube video (implies remote mode; "
            "the video is matched against the playlist for numbering)"
        ),
    )
    parser.add_argument(
        "--slides",
        action="store_true",
        help=(
            "After transcription run extract_slides.py and "
            "text_from_slides.py per video, so that a following --edit "
            "builds the document from the slides; needs the video file "
            "itself, which remote mode does not download"
        ),
    )
    parser.add_argument(
        "--edit",
        action="store_true",
        help=(
            "After transcription run create_final_docs.py per video, "
            "skipping the slide stages unless --slides is given too (the ToC "
            "then comes from the video timecodes); the editing stage asks "
            "its own cost confirmation. "
            "In remote mode the next video is the next *unedited* one: "
            "already transcribed videos go straight to editing"
        ),
    )
    parser.add_argument(
        "--annotate",
        action="store_true",
        help=(
            "With --edit: also create the 200-250 word annotation .txt "
            "(Russian original -> Russian annotation; other originals -> "
            "English + Russian annotations); see create_final_docs.py"
        ),
    )
    parser.add_argument(
        "--terms",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "Course glossary handed to the recognizer, one term per line "
            f"(default: {GLOSSARY_FILENAME} of the playlist folder, then of "
            "the channel folder)"
        ),
    )
    parser.add_argument(
        "--cookies-from-browser",
        default=None,
        metavar="BROWSER",
        help=(
            "Take YouTube cookies from this browser (firefox / chrome / "
            "edge) when remote audio download is refused; the browser "
            "must be closed"
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
        help="Skip the per-video cost confirmation prompt",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=WORKSPACE_ROOT,
        help="Workspace root (default: parent of src/)",
    )
    parser.add_argument(
        "--ffmpeg", default="ffmpeg", help="ffmpeg executable (default: ffmpeg)"
    )
    parser.add_argument(
        "--ffprobe", default="ffprobe", help="ffprobe executable (default: ffprobe)"
    )
    parser.add_argument(
        "--settings",
        default=None,
        metavar="FILE",
        type=Path,
        help=(
            "Text file with CHANNEL / PLAYLIST / TITLE_SUBSTR / "
            "COOKIES_FROM_BROWSER; this is how the channel bats pass values "
            "that cmd.exe cannot carry itself. Command-line values win over it"
        ),
    )
    return parser.parse_args(argv)


SETTINGS_KEYS = (
    "CHANNEL",
    "PLAYLIST",
    "TITLE_SUBSTR",
    "COOKIES_FROM_BROWSER",
)


def apply_run_settings(args: argparse.Namespace) -> None:
    """Fill channel / playlist / title filter from the bat's settings file."""
    if not args.settings:
        if not (args.channel_folder or "").strip():
            raise SystemExit(
                "channel_folder is required (pass it or --settings)."
            )
        return
    settings = read_settings(args.settings, SETTINGS_KEYS)
    print(f"Settings: {args.settings}", flush=True)
    if not (args.channel_folder or "").strip():
        args.channel_folder = reconcile_channel_ref(
            channels_dir(args.workspace),
            args.settings,
            settings.get("CHANNEL", ""),
        )
    if args.playlist_folder is None:
        playlist = settings.get("PLAYLIST", "").strip()
        args.playlist_folder = playlist or None
    if not args.title_substr:
        substr = settings.get("TITLE_SUBSTR", "")
        args.title_substr = substr or None
    if not args.cookies_from_browser:
        browser = settings.get("COOKIES_FROM_BROWSER", "").strip()
        args.cookies_from_browser = browser or None
    if not (args.channel_folder or "").strip():
        raise SystemExit(
            f"CHANNEL is empty. Set it in {args.settings}."
        )
    print(f"Channel: {args.channel_folder}", flush=True)
    if args.playlist_folder:
        print(f"Playlist: {args.playlist_folder}", flush=True)
    if args.title_substr:
        print(f"Title substring: {args.title_substr}", flush=True)
    if args.cookies_from_browser:
        print(f"Cookies from browser: {args.cookies_from_browser}", flush=True)


def local_session(
    args: argparse.Namespace,
    *,
    lang: str,
    needs_en: bool,
    channel_dir: Path,
    playlist_dir: Path,
    orig_dir: Path,
    en_dir: Path,
    api_key: str,
    usd_per_minute: float,
) -> list[Path]:
    """Transcribe the next downloaded video(s); return the ones done."""
    videos = list_videos(playlist_dir)
    if not videos:
        raise SystemExit(f"No video/audio files found in {playlist_dir}")
    session = select_session_videos(videos, orig_dir, en_dir, needs_en, args.next_count)

    done_count = len(videos) - len(
        [v for v in videos if not is_transcribed(v, orig_dir, en_dir, needs_en)]
    )
    print(
        f"Videos: {len(videos)} total, {done_count} already transcribed, "
        f"{len(session)} in this session (--next {next_label(args.next_count)}).",
        flush=True,
    )
    if not session:
        print("Nothing to do: all videos are transcribed.", flush=True)
        return []
    print("Press 'p' to stop after the current video.", flush=True)

    watcher = PauseWatcher()
    calibration = ProgressCalibration()
    passes = 2 if needs_en else 1
    total_minutes = 0.0
    done: list[Path] = []
    for video in session:
        print(f"[{len(done) + 1}/{len(session)}] {video.name}", flush=True)
        if not confirm_video(
            video, args.ffprobe, usd_per_minute, passes, auto_yes=args.yes
        ):
            print("Session stopped: transcription not confirmed.", flush=True)
            break
        total_minutes += transcribe_video(
            video,
            api_key=api_key,
            lang=lang,
            needs_en=needs_en,
            orig_dir=orig_dir,
            en_dir=en_dir,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            calibration=calibration,
            prompt=glossary_prompt(playlist_dir, channel_dir, args.terms),
        )
        done.append(video)
        if watcher.pause_requested() and len(done) < len(session):
            print("Pause requested: stopping after the current video.", flush=True)
            break

    print(
        f"Session done: {len(done)} video(s), {total_minutes:.1f} audio "
        f"minutes, estimated cost "
        f"${total_minutes * usd_per_minute * passes:.2f}.",
        flush=True,
    )
    return done


def local_video_named(channel_dir: Path, folder: str, stem: str) -> bool:
    """Is the video file itself on disk? The slide stages read the frames,
    and remote transcription downloads only the audio track."""
    playlist_dir = channel_dir / "_playlists" / folder
    return any(video.stem == stem for video in list_videos(playlist_dir))


def run_slide_stages(
    args: argparse.Namespace,
    jobs: list[tuple[str, str]],
    channel_dir: Path,
) -> int:
    """Chain extract_slides.py and text_from_slides.py per (playlist folder,
    stem) pair (--slides). With --edit after them the document takes its
    structure from the slides instead of the video timecodes; without --edit
    the run stops here, with slides.json ready for a later editing pass."""
    from slides import extract_slides
    from slides import text_from_slides
    for folder, stem in jobs:
        if not local_video_named(channel_dir, folder, stem):
            print("", flush=True)
            print(
                f"  WARNING: {stem} is not on disk, so its slides cannot be "
                "extracted; download the video "
                "(src\\download\\download_videos.py) and run the slide "
                "stages again.",
                flush=True,
            )
            continue
        argv = [args.channel_folder, folder, "--video", stem]
        print("", flush=True)
        print(f"=== Slides (extract_slides): {stem} ===", flush=True)
        code = extract_slides.main(argv)
        if code:
            return code
        print("", flush=True)
        print(f"=== Slide text (text_from_slides): {stem} ===", flush=True)
        code = text_from_slides.main(argv + (["--yes"] if args.yes else []))
        if code:
            return code
    return 0


def run_final_editing(args: argparse.Namespace, count: int) -> int:
    """Chain create_final_docs.py for the just-transcribed videos (--edit,
    local mode: same playlist folder, next N pending videos)."""
    from docs import create_final_docs
    argv = [
        args.channel_folder,
        args.playlist_folder,
        "--next", str(count),
        "--no-slides",
    ]
    if args.orig_only:
        argv.append("--orig-only")
    if args.annotate:
        argv.append("--annotate")
    if args.terms:
        argv += ["--terms", str(args.terms)]
    if args.yes:
        argv.append("--yes")
    print("", flush=True)
    print("=== Final editing (create_final_docs) ===", flush=True)
    return create_final_docs.main(argv)


def run_final_editing_jobs(
    args: argparse.Namespace, edit_jobs: list[tuple[str, str]]
) -> int:
    """Chain create_final_docs.py per (playlist folder, stem) pair (--edit,
    remote mode: each video is targeted explicitly via --video)."""
    from docs import create_final_docs
    for folder, stem in edit_jobs:
        argv = [
            args.channel_folder,
            folder,
            "--no-slides",
            "--video", stem,
        ]
        if args.orig_only:
            argv.append("--orig-only")
        if args.annotate:
            argv.append("--annotate")
        if args.terms:
            argv += ["--terms", str(args.terms)]
        if args.yes:
            argv.append("--yes")
        print("", flush=True)
        print(f"=== Final editing (create_final_docs): {stem} ===", flush=True)
        code = create_final_docs.main(argv)
        if code:
            return code
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    apply_run_settings(args)
    lang = normalize_lang(args.lang)
    needs_en = lang != "en" and not args.orig_only
    args.next_count = normalize_next_count(args.next_count)

    for tool, name in ((args.ffmpeg, "ffmpeg"), (args.ffprobe, "ffprobe")):
        if not shutil.which(tool):
            raise SystemExit(f"{name} not found: {tool}")

    remote = bool(args.from_youtube or args.url)
    if args.title_substr and not remote:
        raise SystemExit(
            "--title-substr works only in remote mode (--from-youtube / --url)"
        )
    try:
        channel_dir = require_channel_ref(
            channels_dir(args.workspace), args.channel_folder
        )
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    playlist_dir: Path | None = None
    if args.playlist_folder is not None:
        playlist_dir = channel_dir / "_playlists" / args.playlist_folder
        if not playlist_dir.is_dir():
            playlist_dir.mkdir(parents=True, exist_ok=True)
            print(
                f"Created playlist folder: "
                f"{playlist_dir.relative_to(channel_dir)}",
                flush=True,
            )
    elif not remote:
        raise SystemExit(
            "playlist_folder may be omitted only in remote mode "
            "(--from-youtube / --url)"
        )

    # Refused here rather than after the transcription is paid for: the
    # slide stages read the frames of the video, and remote mode brings home
    # only the audio track.
    if args.slides and remote and not (
        playlist_dir is not None and list_videos(playlist_dir)
    ):
        raise SystemExit(
            "--slides needs the video files themselves, and remote mode "
            "downloads only the audio track. Download the videos first "
            "(src\\download\\download_videos.py) or drop --slides."
        )

    api_key = read_api_key(args.workspace)
    usd_per_minute = get_transcription_rate(args.workspace)

    if playlist_dir is not None:
        print(f"Playlist folder: {playlist_dir}", flush=True)
    else:
        print(f"Channel folder: {channel_dir} (flat video list)", flush=True)

    if remote:
        processed, jobs = remote_session(
            args,
            lang=lang,
            needs_en=needs_en,
            channel_dir=channel_dir,
            playlist_dir=playlist_dir,
            api_key=api_key,
            usd_per_minute=usd_per_minute,
        )
        if args.slides and jobs:
            code = run_slide_stages(args, jobs, channel_dir)
            if code:
                return code
        if args.edit and jobs:
            return run_final_editing_jobs(args, jobs)
        return 0

    assert playlist_dir is not None
    done = local_session(
        args,
        lang=lang,
        needs_en=needs_en,
        channel_dir=channel_dir,
        playlist_dir=playlist_dir,
        orig_dir=playlist_dir / lang.upper(),
        en_dir=playlist_dir / "EN",
        api_key=api_key,
        usd_per_minute=usd_per_minute,
    )
    if args.slides and done:
        code = run_slide_stages(
            args,
            [(args.playlist_folder, video.stem) for video in done],
            channel_dir,
        )
        if code:
            return code
    if args.edit and done:
        return run_final_editing(args, len(done))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
