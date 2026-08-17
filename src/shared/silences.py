#!/usr/bin/env python3
"""Where the speaker stopped talking - the real pauses of a recording.

The .srt of whisper is no help here: it butts every segment against the next
one, so the silence between two sentences is 0.0 s on paper whatever it was
in the room (in this course a gap of 1.5 s shows up at 1.2% of the joins).
Yet the pauses are the one honest signal of where a paragraph ends, and they
are still in the audio: ffmpeg finds a few hundred of them per lecture in
about a minute and a half of CPU and not a single token.

The result is cached in a sidecar next to the media file, so that minute is
paid once:

    01_Introduction.mp4  ->  01_Introduction.silences.json

Run as a script it fills those sidecars for a whole playlist:

  python src/shared/silences.py _Autotesting lectures
  python src/shared/silences.py _Autotesting lectures --refresh
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SILENCE_SUFFIX = ".silences.json"
# Everything quieter than this counts as silence. -30 dB keeps the breaths
# and the room tone out while catching the stops between sentences.
SILENCE_NOISE_DB = -30.0
# Shorter than this is a breath, not a pause worth writing down.
SILENCE_MIN_SECONDS = 0.35
# How far from a silence a segment boundary may sit and still count as the
# same spot: whisper rounds its timings, and speech fades into a pause
# rather than stopping dead.
SILENCE_TOLERANCE_SECONDS = 0.7

SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([\d.]+)")


@dataclass(frozen=True)
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def middle(self) -> float:
        return (self.start + self.end) / 2


def silences_path(media: Path) -> Path:
    return media.with_name(media.stem + SILENCE_SUFFIX)


def detect_silences(
    media: Path,
    *,
    ffmpeg: str = "ffmpeg",
    noise_db: float = SILENCE_NOISE_DB,
    min_seconds: float = SILENCE_MIN_SECONDS,
) -> list[Silence]:
    """Run the silence detector of ffmpeg over the audio of one file.

    Decoding the audio alone, this runs some 40x faster than the recording
    plays - about a minute and a half for a lecture of an hour.
    """
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-nostats",
            "-i", str(media),
            "-vn",
            "-af", f"silencedetect=noise={noise_db}dB:d={min_seconds}",
            "-f", "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed for {media.name}:\n{result.stderr[-2000:]}"
        )
    found: list[Silence] = []
    start: float | None = None
    for line in result.stderr.splitlines():
        opened = SILENCE_START_RE.search(line)
        if opened:
            start = max(float(opened.group(1)), 0.0)
            continue
        closed = SILENCE_END_RE.search(line)
        if closed and start is not None:
            found.append(Silence(start, float(closed.group(1))))
            start = None
    return found


def write_silences(
    media: Path, found: list[Silence], *, noise_db: float, min_seconds: float
) -> Path:
    path = silences_path(media)
    path.write_text(
        json.dumps(
            {
                "source": media.name,
                "noise_db": noise_db,
                "min_seconds": min_seconds,
                "silences": [
                    [round(item.start, 3), round(item.end, 3)]
                    for item in found
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def read_silences(
    media: Path, *, noise_db: float, min_seconds: float
) -> list[Silence] | None:
    """The cached silences, or None when there are none for these settings."""
    path = silences_path(media)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        abs(float(data.get("noise_db", 0)) - noise_db) > 1e-6
        or abs(float(data.get("min_seconds", 0)) - min_seconds) > 1e-6
    ):
        return None
    return [Silence(float(a), float(b)) for a, b in data.get("silences") or []]


def load_silences(
    media: Path,
    *,
    ffmpeg: str = "ffmpeg",
    noise_db: float = SILENCE_NOISE_DB,
    min_seconds: float = SILENCE_MIN_SECONDS,
    refresh: bool = False,
) -> list[Silence]:
    """The silences of a recording, detected once and kept in the sidecar.

    An empty list when the media file is missing or ffmpeg is not there: the
    pauses only make the paragraphs better, nothing depends on them.
    """
    if not refresh:
        cached = read_silences(
            media, noise_db=noise_db, min_seconds=min_seconds
        )
        if cached is not None:
            return cached
    if not media.is_file():
        return []
    try:
        found = detect_silences(
            media, ffmpeg=ffmpeg, noise_db=noise_db, min_seconds=min_seconds
        )
    except (OSError, RuntimeError):
        return []
    write_silences(media, found, noise_db=noise_db, min_seconds=min_seconds)
    return found


class SilenceIndex:
    """The silences of one recording, asked about by the moment in time."""

    def __init__(
        self,
        found: list[Silence],
        tolerance: float = SILENCE_TOLERANCE_SECONDS,
    ) -> None:
        self.silences = sorted(found, key=lambda item: item.start)
        self.tolerance = tolerance
        self._starts = [item.start for item in self.silences]

    def __bool__(self) -> bool:
        return bool(self.silences)

    def at(self, moment: float) -> Silence | None:
        """The longest silence that moment falls into, give or take the
        tolerance."""
        best: Silence | None = None
        position = bisect_left(self._starts, moment - self.tolerance)
        for item in self.silences[max(position - 1, 0):]:
            if item.start > moment + self.tolerance:
                break
            if item.start - self.tolerance <= moment <= item.end + self.tolerance:
                if best is None or item.duration > best.duration:
                    best = item
        return best

    def duration_at(self, moment: float) -> float:
        found = self.at(moment)
        return found.duration if found else 0.0

    def between(self, start: float, end: float) -> list[Silence]:
        return [item for item in self.silences if start <= item.middle < end]


# --------------------------------------------------------------------------
# Command line: fill the sidecars of a playlist


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect and cache the pauses of every video of a playlist."
    )
    parser.add_argument("channel", help="Channel folder (e.g. _Autotesting)")
    parser.add_argument("playlist", help="Playlist folder name")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Detect again even when a sidecar is already there",
    )
    parser.add_argument(
        "--ffmpeg", default="ffmpeg", help="ffmpeg executable (default: ffmpeg)"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    from shared.project_paths import channels_dir, require_channel_ref
    from transcribe.transcribe_videos import list_videos

    args = parse_args(argv)
    try:
        channel_dir = require_channel_ref(channels_dir(None), args.channel)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    playlist_dir = channel_dir / "_playlists" / args.playlist
    if not playlist_dir.is_dir():
        raise SystemExit(f"Playlist not found: {playlist_dir}")

    videos = list_videos(playlist_dir)
    if not videos:
        raise SystemExit(f"No media files in {playlist_dir}")

    for number, video in enumerate(videos, start=1):
        cached = read_silences(
            video, noise_db=SILENCE_NOISE_DB, min_seconds=SILENCE_MIN_SECONDS
        )
        if cached is not None and not args.refresh:
            print(
                f"[{number}/{len(videos)}] {video.name}: {len(cached)} "
                "pause(s), cached.",
                flush=True,
            )
            continue
        print(f"[{number}/{len(videos)}] {video.name}: listening...", flush=True)
        started = time.monotonic()
        found = load_silences(video, ffmpeg=args.ffmpeg, refresh=True)
        spent = time.monotonic() - started
        total = sum(item.duration for item in found)
        print(
            f"    {len(found)} pause(s), {total / 60:.1f} min of silence, "
            f"detected in {spent:.0f} s.",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
