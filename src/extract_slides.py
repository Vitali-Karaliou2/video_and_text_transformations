#!/usr/bin/env python3
"""Extract slide images from slide-based videos (PySceneDetect).

Videos live in _channels/<channel>/_playlists/<playlist>/ and are processed
in file-name order, like transcribe_videos.py. For each video a scene change
(slide flip) is detected and one image per scene is saved to
<playlist>/SLIDES/<short-key>/slide_NNN.png together with scenes.csv
(scene numbers and timecodes, useful to align slides with .srt transcripts).

The short key is the shortest leading substring of the video file stem that
uniquely identifies that video among all videos in the playlist (often just
the leading index, e.g. "01", "02"). This keeps paths short enough for
Windows tools to open scenes.csv reliably.

The session start point is found by scanning SLIDES/: the first video without
a result folder is processed first. Images are written to a temporary folder
and renamed on completion, so an interrupted video is redone on the next run.

Examples:
  python src/extract_slides.py _Autotesting lectures
  python src/extract_slides.py _Autotesting lectures --next 3 --threshold 20
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from project_paths import WORKSPACE_ROOT, channels_dir
from transcribe_videos import list_videos

from scenedetect import ContentDetector, SceneManager, open_video
from scenedetect.scene_manager import save_images, write_scene_list

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SLIDES_DIRNAME = "SLIDES"
DEFAULT_THRESHOLD = 27.0
DEFAULT_MIN_SCENE_SECONDS = 4.0
PROGRESS_INTERVAL = 10.0
INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_folder_name(name: str) -> str:
    """Make a string safe as a Windows folder name."""
    cleaned = INVALID_FOLDER_CHARS.sub("_", name).rstrip(" .")
    return cleaned or "_"


def short_slide_keys(stems: list[str]) -> dict[str, str]:
    """Map each video stem to the shortest unique leading substring.

    Among all stems in a playlist, find the minimal prefix length such that
    the sanitized prefixes remain unique. Typical result for numbered files
    like ``01_Introduction.mp4`` / ``02_Locators.mp4`` is ``{"01_…": "01", …}``.
    """
    if not stems:
        return {}
    if len(set(stems)) != len(stems):
        raise ValueError("Video stems in the playlist are not unique")

    max_len = max(len(stem) for stem in stems)
    for length in range(1, max_len + 1):
        keys = [sanitize_folder_name(stem[:length]) for stem in stems]
        if len(set(keys)) == len(stems) and all(keys):
            return dict(zip(stems, keys))
    return {stem: sanitize_folder_name(stem) for stem in stems}


def slides_out_dir(slides_dir: Path, video: Path, keys: dict[str, str]) -> Path:
    return slides_dir / keys[video.stem]


def is_extracted(video: Path, slides_dir: Path, keys: dict[str, str]) -> bool:
    out_dir = slides_out_dir(slides_dir, video, keys)
    return out_dir.is_dir() and any(out_dir.iterdir())


def rename_existing_slide_folders(
    playlist_dir: Path, videos: list[Path]
) -> list[tuple[str, str]]:
    """Rename legacy SLIDES/<full-stem>/ folders to SLIDES/<short-key>/.

    Also accepts already-short folders that match the new key (no-op).
    Returns list of (old_name, new_name) renames performed.
    """
    slides_dir = playlist_dir / SLIDES_DIRNAME
    if not slides_dir.is_dir():
        return []
    keys = short_slide_keys([video.stem for video in videos])
    renames: list[tuple[str, str]] = []
    for video in videos:
        short = keys[video.stem]
        target = slides_dir / short
        candidates = [
            slides_dir / video.stem,
            slides_dir / sanitize_folder_name(video.stem),
        ]
        for source in candidates:
            if not source.is_dir() or source.resolve() == target.resolve():
                continue
            if target.exists():
                raise SystemExit(
                    f"Cannot rename {source.name!r} -> {short!r}: "
                    f"target already exists in {slides_dir}"
                )
            source.rename(target)
            renames.append((source.name, short))
            break
    return renames


class ScanProgressStream:
    """Wraps a VideoStream and prints scanning progress every 10 seconds.

    Unlike the built-in tqdm bar, this prints whole lines, so it is equally
    readable in the console and in bat logs. The percentage is real: it is
    the fraction of video frames already scanned.
    """

    def __init__(self, stream, interval: float = PROGRESS_INTERVAL):
        self._stream = stream
        self._interval = interval
        self._frames_read = 0
        self._total_frames = max(1, stream.duration.frame_num)
        self._last_report = time.monotonic()

    def read(self, *args, **kwargs):
        frame = self._stream.read(*args, **kwargs)
        self._frames_read += 1
        now = time.monotonic()
        if now - self._last_report >= self._interval:
            self._last_report = now
            percent = min(99, 100 * self._frames_read // self._total_frames)
            position = self._stream.position.get_timecode()[:8]
            print(f"    Scanning: {percent}%  ({position})", flush=True)
        return frame

    def __getattr__(self, name):
        return getattr(self._stream, name)


def extract_video_slides(
    video: Path,
    out_dir: Path,
    *,
    threshold: float,
    min_scene_seconds: float,
    image_format: str,
) -> int:
    """Detect slide changes and save one image per scene; return scene count."""
    stream = open_video(str(video))
    manager = SceneManager()
    manager.add_detector(
        ContentDetector(
            threshold=threshold, min_scene_len=f"{min_scene_seconds}s"
        )
    )
    total_minutes = stream.duration.seconds / 60.0
    print(
        f"  Scanning {stream.duration.frame_num} frames"
        f" ({total_minutes:.0f} min of video)...",
        flush=True,
    )
    manager.detect_scenes(ScanProgressStream(stream), show_progress=False)
    scenes = manager.get_scene_list()
    if not scenes:
        # No cuts detected: treat the whole video as a single slide.
        scenes = [(stream.base_timecode, stream.duration)]

    tmp_dir = out_dir.with_name(out_dir.name + ".tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)
    # encoder_param: PNG compression level 0-9; quality 0-100 for jpg/webp.
    save_images(
        scenes,
        stream,
        num_images=1,
        image_extension=image_format,
        encoder_param=6 if image_format == "png" else 95,
        image_name_template="slide_$SCENE_NUMBER",
        output_dir=str(tmp_dir),
        show_progress=False,
    )
    with (tmp_dir / "scenes.csv").open("w", encoding="utf-8", newline="") as csv_file:
        write_scene_list(csv_file, scenes)

    shutil.rmtree(out_dir, ignore_errors=True)
    tmp_dir.rename(out_dir)
    return len(scenes)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract slide images from slide-based videos."
    )
    parser.add_argument(
        "channel_folder",
        help="Channel folder name under _channels (e.g. _Autotesting)",
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
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=(
            "Scene change sensitivity, lower = more sensitive "
            f"(default: {DEFAULT_THRESHOLD})"
        ),
    )
    parser.add_argument(
        "--min-scene-seconds",
        type=float,
        default=DEFAULT_MIN_SCENE_SECONDS,
        help=(
            "Minimum slide duration in seconds; shorter changes are merged "
            f"(default: {DEFAULT_MIN_SCENE_SECONDS})"
        ),
    )
    parser.add_argument(
        "--image-format",
        choices=("png", "jpg", "webp"),
        default="png",
        help="Slide image format (default: png)",
    )
    parser.add_argument(
        "--rename-existing",
        action="store_true",
        help=(
            "Only rename existing SLIDES/<full-stem>/ folders to short unique "
            "keys; do not extract new slides"
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
    if args.next_count < 1:
        raise SystemExit("--next must be a positive number")

    playlist_dir = (
        channels_dir(args.workspace)
        / args.channel_folder
        / "_playlists"
        / args.playlist_folder
    )
    if not playlist_dir.is_dir():
        raise SystemExit(f"Playlist folder not found: {playlist_dir}")

    slides_dir = playlist_dir / SLIDES_DIRNAME
    videos = list_videos(playlist_dir)
    if not videos:
        raise SystemExit(f"No video/audio files found in {playlist_dir}")

    keys = short_slide_keys([video.stem for video in videos])
    sample = next(iter(keys.values()))
    print(f"Playlist folder: {playlist_dir}", flush=True)
    print(
        f"Slide folder keys: length {len(sample)} "
        f"(e.g. {', '.join(list(keys.values())[:3])}…)",
        flush=True,
    )

    if args.rename_existing:
        renames = rename_existing_slide_folders(playlist_dir, videos)
        if not renames:
            print("No SLIDES folders needed renaming.", flush=True)
        else:
            for old, new in renames:
                print(f"  Renamed: {old}  ->  {new}", flush=True)
            print(f"Renamed {len(renames)} folder(s).", flush=True)
        return 0

    pending = [
        video for video in videos if not is_extracted(video, slides_dir, keys)
    ]
    session = pending[: args.next_count]

    print(
        f"Videos: {len(videos)} total, {len(videos) - len(pending)} already done, "
        f"{len(session)} in this session (--next {args.next_count}).",
        flush=True,
    )
    if not session:
        print("Nothing to do: slides are extracted for all videos.", flush=True)
        return 0

    for index, video in enumerate(session, start=1):
        print(f"[{index}/{len(session)}] {video.name}", flush=True)
        out_dir = slides_out_dir(slides_dir, video, keys)
        count = extract_video_slides(
            video,
            out_dir,
            threshold=args.threshold,
            min_scene_seconds=args.min_scene_seconds,
            image_format=args.image_format,
        )
        print(
            f"  Saved: {out_dir.relative_to(playlist_dir)} "
            f"({count} slide(s) + scenes.csv)",
            flush=True,
        )

    print(f"Session done: {len(session)} video(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
