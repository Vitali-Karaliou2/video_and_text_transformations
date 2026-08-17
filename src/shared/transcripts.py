#!/usr/bin/env python3
"""A transcript as data: timed segments, paragraphs, and the .srt around them.

The recognizer hands back segments; the .txt, the checks and the editing
stage all work from those, so where a paragraph ends and how good the
recognition was are decided here rather than in any one stage.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

from shared.silences import SILENCE_MIN_SECONDS, SilenceIndex

TXT_WRAP_WIDTH = 80
# A silence this long between segments starts a new paragraph in the .txt.
# Only whisper writes almost no silence into an .srt (it butts the segments
# against each other), so this fires at about 1% of the joins and the real
# work is done by the pauses of silences.py wherever they are available.
PARAGRAPH_GAP_SECONDS = 1.5
PARAGRAPH_MAX_CHARS = 1000
# Paragraphs built on the real pauses: shorter than this a paragraph is a
# stub, longer than this it is a wall of text, and in between the length is
# decided by how long the speaker stopped.
PARAGRAPH_MIN_WORDS = 45
PARAGRAPH_MAX_WORDS = 130
# Whisper sometimes runs for five minutes on commas alone, without a single
# full stop; past this many words a pause ends the paragraph even in the
# middle of such a "sentence" (the editor repunctuates the text anyway).
PARAGRAPH_HARD_WORDS = 200
# ...and past this many the demand for a pause is dropped as well: the join
# between two segments is taken as it comes. A stretch with neither a full
# stop nor a silence of its own is rare (one or two per lecture), but it is
# what was still handing the editor blocks of 300 words.
PARAGRAPH_LAST_RESORT_WORDS = 260
# The pause a sentence end must be followed by to break a paragraph: this
# long right after the minimum, falling to SILENCE_MIN_SECONDS as the
# paragraph approaches the maximum.
PARAGRAPH_STRONG_PAUSE = 0.8
# Sidecar with the per-segment recognition quality (see write_asr_meta in
# transcribe/transcribe_videos.py).
ASR_META_SUFFIX = ".asr.json"
LANGUAGE_NAMES = {
    "RU": "Russian", "EN": "English", "DE": "German", "FR": "French",
    "ES": "Spanish", "IT": "Italian", "PL": "Polish", "UK": "Ukrainian",
}


@dataclass
class Segment:
    start: float
    end: float
    text: str
    # Recognition quality of the segment, as reported by the API in
    # verbose_json; None for segments read back from an .srt file.
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None


def optional_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def normalize_lang(value: str) -> str:
    lang = value.strip().lower()
    if len(lang) != 2 or not lang.isalpha():
        raise SystemExit(
            f"--lang must be a two-letter language code, got: {value!r}"
        )
    return lang


def srt_timestamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def segments_to_srt(segments: list[Segment]) -> str:
    blocks = [
        f"{index}\n{srt_timestamp(seg.start)} --> {srt_timestamp(seg.end)}\n"
        f"{seg.text}\n"
        for index, seg in enumerate(segments, start=1)
    ]
    return "\n".join(blocks)


def ends_sentence(text: str) -> bool:
    tail = text.rstrip().rstrip("\"\u00bb)]")
    return tail.endswith((".", "!", "?", "\u2026"))


def pause_wanted(
    words: int,
    min_words: int = PARAGRAPH_MIN_WORDS,
    max_words: int = PARAGRAPH_MAX_WORDS,
) -> float:
    """How long a pause has to be to end a paragraph of that many words.

    Right after the minimum only a clear stop will do; the closer the
    paragraph comes to the maximum, the shorter a pause is enough.
    """
    if words < min_words:
        return float("inf")
    if words >= max_words:
        return 0.0
    share = (words - min_words) / (max_words - min_words)
    return PARAGRAPH_STRONG_PAUSE - share * (
        PARAGRAPH_STRONG_PAUSE - SILENCE_MIN_SECONDS
    )


def group_paragraphs_by_pauses(
    segments: list[Segment],
    pauses: SilenceIndex,
    min_words: int = PARAGRAPH_MIN_WORDS,
    max_words: int = PARAGRAPH_MAX_WORDS,
) -> list[str]:
    """Group segments into paragraphs where the speaker really stopped.

    A paragraph may only end where a sentence does, and of the sentence ends
    it takes the ones the speaker paused at - insisting on a long pause at
    first and settling for any as the paragraph grows. Past the maximum the
    next sentence end ends it whether or not there was a pause: some
    stretches (a demo, a read-out list) are spoken without a single one.
    Past PARAGRAPH_HARD_WORDS even a sentence end is no longer waited for -
    a pause alone will do, because the recognizer can go for minutes on
    commas and would otherwise hand over one paragraph of 600 words; and
    past PARAGRAPH_LAST_RESORT_WORDS the pause is not waited for either.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    words = 0
    for segment, following in zip(segments, segments[1:] + [None]):
        text = segment.text.strip()
        if text:
            current.append(text)
            words += len(text.split())
        if following is None or not current:
            continue
        pause = pauses.duration_at(following.start)
        if ends_sentence(segment.text):
            wanted = pause_wanted(words, min_words, max_words)
        elif words >= PARAGRAPH_HARD_WORDS:
            # The demand for a pause fades out the same way: a short one
            # right past the cap, none at all by the last resort.
            over = (words - PARAGRAPH_HARD_WORDS) / (
                PARAGRAPH_LAST_RESORT_WORDS - PARAGRAPH_HARD_WORDS
            )
            wanted = SILENCE_MIN_SECONDS * max(0.0, 1.0 - over)
        else:
            continue
        if pause >= wanted:
            paragraphs.append(" ".join(current))
            current = []
            words = 0
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def group_paragraphs(
    segments: list[Segment],
    gap_seconds: float = PARAGRAPH_GAP_SECONDS,
    max_chars: int = PARAGRAPH_MAX_CHARS,
    pauses: SilenceIndex | None = None,
) -> list[str]:
    """Group segments into paragraphs at silence gaps (or after ~max_chars
    at a sentence end, so continuous speech does not become one huge block).

    With the real pauses of the recording at hand the far better rule of
    group_paragraphs_by_pauses is used instead.
    """
    if pauses:
        return group_paragraphs_by_pauses(segments, pauses)
    paragraphs: list[str] = []
    current: list[str] = []
    length = 0
    prev_end: float | None = None
    for seg in segments:
        text = seg.text.strip()
        if current:
            long_pause = (
                prev_end is not None and seg.start - prev_end >= gap_seconds
            )
            overlong = length >= max_chars and current[-1].endswith(
                (".", "!", "?")
            )
            if long_pause or overlong:
                paragraphs.append(" ".join(current))
                current = []
                length = 0
        if text:
            current.append(text)
            length += len(text) + 1
        prev_end = seg.end
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def format_txt(
    text: str,
    segments: list[Segment],
    width: int = TXT_WRAP_WIDTH,
    pauses: SilenceIndex | None = None,
) -> str:
    paragraphs = (
        group_paragraphs(segments, pauses=pauses) if segments else [text]
    )
    blocks = [
        "\n".join(
            textwrap.wrap(
                paragraph,
                width=width,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
        for paragraph in paragraphs
        if paragraph.strip()
    ]
    return "\n\n".join(blocks)


def asr_meta_path(out_dir: Path, stem: str) -> Path:
    return out_dir / f"{stem}{ASR_META_SUFFIX}"


def read_asr_meta(out_dir: Path, stem: str) -> list[Segment]:
    """Segments with their recognition quality; empty when there is no
    sidecar."""
    path = asr_meta_path(out_dir, stem)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        Segment(
            start=float(entry.get("start", 0.0)),
            end=float(entry.get("end", 0.0)),
            text=str(entry.get("text", "")),
            avg_logprob=optional_float(entry.get("avg_logprob")),
            no_speech_prob=optional_float(entry.get("no_speech_prob")),
            compression_ratio=optional_float(entry.get("compression_ratio")),
        )
        for entry in data.get("segments", [])
    ]
