#!/usr/bin/env python3
"""Report the suspicious spots of a transcript - offline, no API calls.

Recognition errors are not spread evenly over a lecture: they cluster on the
English terms of the subject, on names, and on the passages where the speaker
was hard to hear. Three signals, all free, point at those places:

- a word the morphological dictionary does not know ("засанить", "хотки") is
  either a genuine loanword or, more often, a misheard one;
- a Latin word in the original-language transcript that does not occur in the
  English transcript of the same video ("PlevRite" against "Playwright") is
  spelled the way nobody else spells it;
- the recognizer's own confidence for the segment (avg_logprob in the
  <stem>.asr.json sidecar written by transcribe_videos.py), which separates
  "a rare word" from "a word it could barely hear".

Words that mix scripts ("environment-ах", "avoid-ить") are listed separately:
they are not errors but the loanword decisions of the transcript, and seeing
them together is what makes a consistent policy possible.

The report goes to <playlist>/<LANG>/CHECKS/<stem>.txt and its frequency list
doubles as the raw material for the terms.txt glossary.

Examples:
  python src/transcribe/check_transcripts.py IT\\_Autotesting lectures
  python src/transcribe/check_transcripts.py IT\\_Autotesting lectures --video 01_Introduction
  python src/transcribe/check_transcripts.py IT\\_Autotesting lectures --min-logprob -0.5
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from shared.glossary import GLOSSARY_FILENAME, find_glossary, load_terms, term_words
from shared.project_paths import (
    WORKSPACE_ROOT,
    channels_dir,
    require_channel_ref,
)
from shared.transcripts import Segment, normalize_lang, read_asr_meta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CHECKS_DIRNAME = "CHECKS"
# Languages pymorphy3 has a dictionary for.
MORPHOLOGY_LANGS = {"ru", "uk"}
# whisper reports about -0.15 for clean speech; below this it was guessing.
DEFAULT_MIN_LOGPROB = -0.45
DEFAULT_MAX_NO_SPEECH = 0.5
# Two letters are function words, and their recognition rarely goes wrong.
MIN_WORD_LENGTH = 3
# A glossary term is matched by prefix, so its inflected forms count as known.
MIN_TERM_PREFIX = 4
# An unknown word said more often than this across the playlist belongs to
# the speaker, not to the recognizer's mistakes.
DEFAULT_RARE_COUNT = 2
TOP_WORDS = 40

WORD_RE = re.compile(r"[^\W\d_]+(?:[-'\u2019][^\W\d_]+)*", re.UNICODE)
CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
LATIN_RE = re.compile(r"[A-Za-z]")


@dataclass
class Flags:
    """What is suspicious about one segment."""

    unknown: list[str] = field(default_factory=list)
    vocabulary: list[str] = field(default_factory=list)
    latin: list[str] = field(default_factory=list)
    mixed: list[str] = field(default_factory=list)
    low_confidence: bool = False

    @property
    def reportable(self) -> bool:
        """A word the speaker keeps using is their vocabulary, not an error;
        mixed-script words are a style note. Neither makes a segment suspect."""
        return bool(self.unknown or self.latin or self.low_confidence)


# --------------------------------------------------------------------------
# Inputs


def parse_srt(path: Path) -> list[Segment]:
    def to_seconds(stamp: str) -> float:
        hours, minutes, rest = stamp.strip().split(":")
        seconds, millis = rest.split(",")
        return (
            int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000.0
        )

    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig")):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        start, end = lines[1].split("-->")
        segments.append(
            Segment(
                start=to_seconds(start),
                end=to_seconds(end),
                text=" ".join(line.strip() for line in lines[2:]),
            )
        )
    return segments


def load_segments(lang_dir: Path, stem: str) -> list[Segment]:
    """Segments with confidence when the sidecar is there, from the .srt if not."""
    segments = read_asr_meta(lang_dir, stem)
    if segments:
        return segments
    srt_path = lang_dir / f"{stem}.srt"
    return parse_srt(srt_path) if srt_path.is_file() else []


def transcript_stems(lang_dir: Path) -> list[str]:
    return sorted(path.stem for path in lang_dir.glob("*.srt"))


def words(text: str) -> list[str]:
    return WORD_RE.findall(text)


def latin_vocabulary(segments: list[Segment]) -> set[str]:
    """Every Latin word of the English transcript, lower-cased."""
    vocabulary: set[str] = set()
    for segment in segments:
        for word in words(segment.text):
            if LATIN_RE.search(word) and not CYRILLIC_RE.search(word):
                vocabulary.add(word.lower())
    return vocabulary


# --------------------------------------------------------------------------
# Checks


class Dictionary:
    """Known-word test: the morphological dictionary plus the course glossary."""

    def __init__(self, lang: str, terms: list[str]) -> None:
        self.terms = sorted(term_words(terms))
        self.analyzer = None
        self.note = ""
        if lang not in MORPHOLOGY_LANGS:
            self.note = (
                f"no morphological dictionary for '{lang}': unknown-word check "
                "skipped"
            )
            return
        try:
            import pymorphy3
        except ImportError:
            self.note = (
                "pymorphy3 is not installed: unknown-word check skipped "
                "(pip install -r src/requirements.txt)"
            )
            return
        self.analyzer = pymorphy3.MorphAnalyzer(lang=lang)

    def in_glossary(self, word: str) -> bool:
        lowered = word.lower()
        return any(
            lowered == term
            or (len(term) >= MIN_TERM_PREFIX and lowered.startswith(term))
            for term in self.terms
        )

    def knows(self, word: str) -> bool:
        if self.analyzer is None:
            return True
        return any(parse.is_known for parse in self._parse(word.lower()))

    def lemma(self, word: str) -> str:
        """Normal form, so that "поинты" and "поинтов" count as one word."""
        lowered = word.lower()
        if self.analyzer is None:
            return lowered
        parses = self._parse(lowered)
        return parses[0].normal_form if parses else lowered

    @lru_cache(maxsize=100_000)
    def _parse(self, word: str) -> tuple:
        return tuple(self.analyzer.parse(word))  # type: ignore[union-attr]


def speaker_vocabulary(
    lang_dir: Path, stems: list[str], dictionary: Dictionary, rare_count: int
) -> set[str]:
    """Words that are outside the dictionary but clearly the speaker's own.

    A mishearing is a one-off; a word the lecturer says in lecture after
    lecture ("тестировщик", "поинты") is simply vocabulary the dictionary
    does not carry, and flagging it every time buries the real errors.
    """
    counts: Counter[str] = Counter()
    for stem in stems:
        for segment in load_segments(lang_dir, stem):
            for word in words(segment.text):
                if len(word) < MIN_WORD_LENGTH or CYRILLIC_RE.search(word) is None:
                    continue
                if LATIN_RE.search(word) or dictionary.knows(word):
                    continue
                counts[dictionary.lemma(word)] += 1
    return {word for word, count in counts.items() if count > rare_count}


def check_segment(
    segment: Segment,
    dictionary: Dictionary,
    english: set[str],
    vocabulary: set[str],
    *,
    min_logprob: float,
    max_no_speech: float,
) -> Flags:
    flags = Flags()
    for word in words(segment.text):
        if len(word) < MIN_WORD_LENGTH or dictionary.in_glossary(word):
            continue
        cyrillic = bool(CYRILLIC_RE.search(word))
        latin = bool(LATIN_RE.search(word))
        if cyrillic and latin:
            flags.mixed.append(word)
        elif latin:
            if word.lower() not in english:
                flags.latin.append(word)
        elif cyrillic and not dictionary.knows(word):
            if dictionary.lemma(word) in vocabulary:
                flags.vocabulary.append(word)
            else:
                flags.unknown.append(word)
    if segment.avg_logprob is not None and segment.avg_logprob < min_logprob:
        flags.low_confidence = True
    if segment.no_speech_prob is not None and segment.no_speech_prob > max_no_speech:
        flags.low_confidence = True
    return flags


# --------------------------------------------------------------------------
# Report


def timestamp(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def confidence_note(segment: Segment) -> str:
    if segment.avg_logprob is None:
        return ""
    return f"  [confidence {segment.avg_logprob:.2f}]"


def frequency_block(title: str, counter: Counter[str], comment: str) -> list[str]:
    if not counter:
        return []
    lines = ["", title, "-" * len(title), comment, ""]
    for word, count in counter.most_common(TOP_WORDS):
        lines.append(f"  {count:>3}  {word}")
    if len(counter) > TOP_WORDS:
        lines.append(f"  ... and {len(counter) - TOP_WORDS} more")
    return lines


def build_report(
    stem: str,
    lang: str,
    segments: list[Segment],
    flagged: list[tuple[Segment, Flags]],
    counters: dict[str, Counter[str]],
    *,
    dictionary: Dictionary,
    english_available: bool,
    has_confidence: bool,
) -> str:
    lines = [
        f"Transcript check: {stem} [{lang.upper()}]",
        "=" * (len(stem) + len(lang) + 21),
        "",
        f"Segments: {len(segments)} total, {len(flagged)} with something to look at.",
        f"Unknown words: {sum(counters['unknown'].values())} "
        f"({len(counters['unknown'])} distinct), plus "
        f"{sum(counters['vocabulary'].values())} occurrence(s) of "
        f"{len(counters['vocabulary'])} recurring word(s) taken as vocabulary.",
        f"Unmatched Latin words: {sum(counters['latin'].values())} "
        f"({len(counters['latin'])} distinct).",
        f"Mixed-script words: {sum(counters['mixed'].values())} "
        f"({len(counters['mixed'])} distinct).",
    ]
    if dictionary.note:
        lines.append(f"NOTE: {dictionary.note}.")
    if not english_available:
        lines.append("NOTE: no English transcript; Latin spelling was not verified.")
    if not has_confidence:
        lines.append(
            "NOTE: no .asr.json sidecar (transcript predates it); recognition "
            "confidence was not available."
        )

    if flagged:
        lines += ["", "Suspicious segments", "-------------------"]
        for segment, flags in flagged:
            marks = []
            if flags.unknown:
                marks.append("unknown: " + ", ".join(flags.unknown))
            if flags.latin:
                marks.append("latin: " + ", ".join(flags.latin))
            if flags.mixed:
                marks.append("mixed: " + ", ".join(flags.mixed))
            if flags.low_confidence:
                marks.append("low confidence")
            lines += [
                "",
                f"[{timestamp(segment.start)}]{confidence_note(segment)}",
                f"  {segment.text}",
                f"  -> {'; '.join(marks)}",
            ]

    lines += frequency_block(
        "Unknown words by frequency",
        counters["unknown"],
        "Rare enough to be a mishearing rather than a word of the course.",
    )
    lines += frequency_block(
        "Latin words absent from the English transcript",
        counters["latin"],
        "Usually a garbled product or technology name.",
    )
    lines += frequency_block(
        "Recurring words outside the dictionary",
        counters["vocabulary"],
        f"The vocabulary of the course - the material for {GLOSSARY_FILENAME}.",
    )
    lines += frequency_block(
        "Loanwords in local morphology",
        counters["mixed"],
        "Not errors: the loanword decisions this transcript makes.",
    )
    return "\n".join(lines) + "\n"


def check_video(
    stem: str,
    *,
    lang: str,
    orig_dir: Path,
    en_dir: Path,
    dictionary: Dictionary,
    vocabulary: set[str],
    min_logprob: float,
    max_no_speech: float,
) -> tuple[str, int, int]:
    """Check one transcript; return (report text, flagged segments, total)."""
    segments = load_segments(orig_dir, stem)
    english = latin_vocabulary(load_segments(en_dir, stem)) if en_dir.is_dir() else set()

    flagged: list[tuple[Segment, Flags]] = []
    counters = {
        "unknown": Counter[str](),
        "vocabulary": Counter[str](),
        "latin": Counter[str](),
        "mixed": Counter[str](),
    }
    for segment in segments:
        flags = check_segment(
            segment,
            dictionary,
            english,
            vocabulary,
            min_logprob=min_logprob,
            max_no_speech=max_no_speech,
        )
        counters["unknown"].update(word.lower() for word in flags.unknown)
        counters["vocabulary"].update(word.lower() for word in flags.vocabulary)
        counters["latin"].update(flags.latin)
        counters["mixed"].update(word.lower() for word in flags.mixed)
        if flags.reportable:
            flagged.append((segment, flags))

    report = build_report(
        stem,
        lang,
        segments,
        flagged,
        counters,
        dictionary=dictionary,
        english_available=bool(english),
        has_confidence=any(seg.avg_logprob is not None for seg in segments),
    )
    return report, len(flagged), len(segments)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report suspicious spots of transcripts (offline, no API)."
    )
    parser.add_argument(
        "channel_folder",
        help="Channel ref under _channels/ (e.g. IT\\_Autotesting)",
    )
    parser.add_argument(
        "playlist_folder",
        help="Playlist folder name under <channel>/_playlists (e.g. lectures)",
    )
    parser.add_argument(
        "--lang",
        default="ru",
        metavar="XX",
        help="Original language, two-letter code (default: ru)",
    )
    parser.add_argument(
        "--video",
        default=None,
        metavar="STEM",
        help="Check this transcript only (default: every one in the folder)",
    )
    parser.add_argument(
        "--terms",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            f"Course glossary whose terms are known words (default: "
            f"{GLOSSARY_FILENAME} of the playlist folder, then of the channel "
            "folder)"
        ),
    )
    parser.add_argument(
        "--rare-count",
        type=int,
        default=DEFAULT_RARE_COUNT,
        metavar="N",
        help=(
            "An unknown word said more than N times across the playlist is the "
            f"speaker's vocabulary, not an error (default: {DEFAULT_RARE_COUNT})"
        ),
    )
    parser.add_argument(
        "--min-logprob",
        type=float,
        default=DEFAULT_MIN_LOGPROB,
        metavar="X",
        help=(
            "Flag segments the recognizer was less sure about than this "
            f"(default: {DEFAULT_MIN_LOGPROB}; needs the .asr.json sidecar)"
        ),
    )
    parser.add_argument(
        "--max-no-speech",
        type=float,
        default=DEFAULT_MAX_NO_SPEECH,
        metavar="X",
        help=(
            "Flag segments more likely than this to be no speech at all "
            f"(default: {DEFAULT_MAX_NO_SPEECH})"
        ),
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
    lang = normalize_lang(args.lang)

    try:
        channel_dir = require_channel_ref(
            channels_dir(args.workspace), args.channel_folder
        )
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    playlist_dir = channel_dir / "_playlists" / args.playlist_folder
    if not playlist_dir.is_dir():
        raise SystemExit(f"Playlist folder not found: {playlist_dir}")
    orig_dir = playlist_dir / lang.upper()
    if not orig_dir.is_dir():
        raise SystemExit(f"Transcript folder not found: {orig_dir}")
    en_dir = playlist_dir / "EN"

    glossary_path = find_glossary(playlist_dir, channel_dir, args.terms)
    terms = load_terms(glossary_path)
    print(
        f"Glossary: {glossary_path} ({len(terms)} term(s))."
        if glossary_path
        else f"Glossary: none ({GLOSSARY_FILENAME} not found).",
        flush=True,
    )
    dictionary = Dictionary(lang, terms)
    if dictionary.note:
        print(f"NOTE: {dictionary.note}.", flush=True)

    available = transcript_stems(orig_dir)
    stems = [args.video] if args.video else available
    if not stems:
        raise SystemExit(f"No .srt transcripts in {orig_dir}")

    checks_dir = orig_dir / CHECKS_DIRNAME
    checks_dir.mkdir(parents=True, exist_ok=True)
    print(f"Transcripts: {len(stems)} of {len(available)} in {orig_dir}", flush=True)

    # Built from every transcript of the playlist, even when only one is
    # checked: one lecture is too little to tell a habit from a slip.
    vocabulary = speaker_vocabulary(orig_dir, available, dictionary, args.rare_count)
    print(
        f"Vocabulary: {len(vocabulary)} recurring word(s) outside the "
        "dictionary are taken as the speaker's own.",
        flush=True,
    )

    for position, stem in enumerate(stems, start=1):
        if not (orig_dir / f"{stem}.srt").is_file():
            raise SystemExit(f"Transcript not found: {orig_dir / (stem + '.srt')}")
        report, flagged, total = check_video(
            stem,
            lang=lang,
            orig_dir=orig_dir,
            en_dir=en_dir,
            dictionary=dictionary,
            vocabulary=vocabulary,
            min_logprob=args.min_logprob,
            max_no_speech=args.max_no_speech,
        )
        report_path = checks_dir / f"{stem}.txt"
        report_path.write_text(report, encoding="utf-8")
        print(
            f"[{position}/{len(stems)}] {stem}: {flagged} of {total} segment(s) "
            f"to look at -> {report_path.relative_to(playlist_dir)}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
