#!/usr/bin/env python3
"""Organize downloaded YouTube videos into playlist folders.

Takes a channel collection folder name (under workspace/output/) and:
1. Detects the YouTube channel (from local video IDs via yt-dlp, or --channel)
2. Fetches channel playlists and maps local *.mp4 files by [video_id] in the name
3. Moves files into nested playlist folders (unmatched -> misc/)
4. Optionally renames files so lexical sort matches playlist order (hybrid strategy
   from the Alexander_Lamkov workflow: pad leading N./#N/Урок N when present;
   otherwise add NN_ playlist-index prefixes)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

VIDEO_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.(?:mp4|mkv|webm)$", re.I)
LEADING_N_DOT = re.compile(r"^(\d)\.(\s)")
LEADING_N_DOT_OK = re.compile(r"^\d{2,}\.\s")
LEADING_HASH_N = re.compile(r"^#(\d)(\s)")
LEADING_HASH_OK = re.compile(r"^#\d{2,}(\s)")
LEADING_LESSON = re.compile(
    r"^(Урок|Lesson|Part|Часть)\s+(\d)([\.\s:])", re.I
)
LEADING_LESSON_OK = re.compile(
    r"^(Урок|Lesson|Part|Часть)\s+\d{2,}([\.\s:])", re.I
)
ALREADY_PREFIX = re.compile(r"^\d{2}_")
COURSE_HINTS = re.compile(
    r"курс|course|урок|lecture|tutorial|мастер.?класс|с\s*нул|"
    r"crash\s*course|full\s*course|roadmap|практик|урок",
    re.I,
)

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIRNAME = "output"


def collection_root(args: argparse.Namespace) -> Path:
    output_base = (
        args.output_dir
        if args.output_dir is not None
        else args.workspace / DEFAULT_OUTPUT_DIRNAME
    )
    return (output_base / args.folder).resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Distribute channel videos into playlist subfolders."
    )
    p.add_argument(
        "folder",
        help="Name of the collection subfolder under output/ "
        "(e.g. Vladilen_Minin)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=f"Parent directory for collections "
        f"(default: <workspace>/{DEFAULT_OUTPUT_DIRNAME})",
    )
    p.add_argument(
        "--channel",
        help="YouTube channel URL or handle "
        "(e.g. https://www.youtube.com/@VladilenMinin). "
        "If omitted, detected from local video metadata via yt-dlp.",
    )
    p.add_argument(
        "--workspace",
        type=Path,
        default=WORKSPACE_ROOT,
        help="Workspace root containing the target folder "
        f"(default: {WORKSPACE_ROOT})",
    )
    p.add_argument(
        "--order-mode",
        choices=("courses", "all", "none"),
        default="courses",
        help="Rename strategy for playlist order: "
        "courses (default) = only course-like playlists; "
        "all = every playlist folder; none = skip renaming.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan moves/renames without changing files.",
    )
    p.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Pass through to yt-dlp (e.g. chrome, firefox, edge).",
    )
    p.add_argument(
        "--yt-dlp",
        default="yt-dlp",
        help="yt-dlp executable name or path (default: yt-dlp).",
    )
    return p.parse_args(argv)


def yt_dlp_cmd(args: argparse.Namespace, *extra: str) -> list[str]:
    cmd = [args.yt_dlp, *extra]
    if args.cookies_from_browser:
        cmd[1:1] = ["--cookies-from-browser", args.cookies_from_browser]
    return cmd


def yt_dlp_flat_json(args: argparse.Namespace, url: str) -> list[dict]:
    proc = subprocess.run(
        yt_dlp_cmd(args, "--flat-playlist", "--dump-json", url),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"yt-dlp failed for {url}:\n{err[:2000]}")
    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def yt_dlp_video_meta(args: argparse.Namespace, video_id: str) -> dict:
    url = f"https://www.youtube.com/watch?v={video_id}"
    proc = subprocess.run(
        yt_dlp_cmd(
            args,
            "--skip-download",
            "--print",
            "%(.{id,channel,channel_id,channel_url,uploader_id,webpage_url})j",
            url,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"yt-dlp metadata failed for {video_id}:\n{err[:1500]}")
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def slugify_playlist_name(title: str, max_len: int = 60) -> str:
    cyr_to_lat = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    title = title.lower()
    result: list[str] = []
    for ch in title:
        if ch in cyr_to_lat:
            result.append(cyr_to_lat[ch])
        elif ch.isalnum():
            result.append(ch)
        else:
            result.append("_")
    slug = re.sub(r"_+", "_", "".join(result)).strip("_")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("_")
    return slug or "unnamed_playlist"


def unique_folder_name(base: str, used: set[str]) -> str:
    name = base
    n = 2
    while name in used:
        suffix = f"_{n}"
        name = (base[: 60 - len(suffix)] + suffix) if len(base) + len(suffix) > 60 else base + suffix
        n += 1
    used.add(name)
    return name


def extract_video_id(filename: str) -> str | None:
    m = VIDEO_ID_RE.search(filename)
    return m.group(1) if m else None


def list_local_videos(root: Path) -> dict[str, Path]:
    """Map video_id -> file for *.mp4/mkv/webm directly in root (not subfolders)."""
    local: dict[str, Path] = {}
    for pattern in ("*.mp4", "*.mkv", "*.webm"):
        for f in root.glob(pattern):
            if not f.is_file():
                continue
            vid = extract_video_id(f.name)
            if vid:
                local[vid] = f
    return local


def win_long(path: Path) -> str:
    s = str(path.resolve())
    if sys.platform == "win32" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


def safe_move(src: Path, dest: Path, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] MOVE {src.name} -> {dest.parent.name}/")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"  SKIP (exists): {dest.name}")
        return
    shutil.move(win_long(src), win_long(dest))


def detect_channel(args: argparse.Namespace, local_files: dict[str, Path]) -> str:
    if args.channel:
        return normalize_channel_playlists_url(args.channel)

    sample_ids = list(local_files.keys())[:5]
    if not sample_ids:
        raise SystemExit(
            "No local videos with [video_id] in the filename found. "
            "Cannot auto-detect channel; pass --channel."
        )

    print("Detecting channel from sample videos...")
    channel_urls: list[str] = []
    channel_ids: list[str] = []
    errors: list[str] = []
    for vid in sample_ids:
        try:
            meta = yt_dlp_video_meta(args, vid)
            curl = meta.get("channel_url") or ""
            cid = meta.get("channel_id") or ""
            print(f"  {vid}: channel={meta.get('channel')!r} id={cid}")
            if curl:
                channel_urls.append(curl.rstrip("/"))
            if cid:
                channel_ids.append(cid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{vid}: {exc}")
            print(f"  {vid}: FAILED ({exc})")

    if channel_ids:
        best_id = Counter(channel_ids).most_common(1)[0][0]
        return f"https://www.youtube.com/channel/{best_id}/playlists"
    if channel_urls:
        best = Counter(channel_urls).most_common(1)[0][0]
        return normalize_channel_playlists_url(best)

    # Fallback: try @FolderName handle
    handle = args.folder.replace("_", "")
    guess = f"https://www.youtube.com/@{handle}/playlists"
    print(f"Metadata lookup failed; trying handle guess: {guess}")
    if errors:
        print("Errors:")
        for e in errors[:3]:
            print(f"  {e}")
    return guess


def normalize_channel_playlists_url(channel: str) -> str:
    c = channel.strip().rstrip("/")
    if c.startswith("@"):
        c = f"https://www.youtube.com/{c}"
    if "/playlists" in c:
        return c
    if "/channel/" in c or "/@" in c or "/c/" in c or "/user/" in c:
        return c + "/playlists"
    # bare channel id
    if re.fullmatch(r"UC[\w-]{22}", c):
        return f"https://www.youtube.com/channel/{c}/playlists"
    return c + "/playlists"


def playlist_priority(
    item: tuple[str, str, int],
    playlist_sizes: dict[str, int],
    numbered_ratio: dict[str, float],
) -> tuple:
    """Prefer specific course playlists when a video appears in several."""
    folder, title, _ = item
    title_l = (title or "").strip().lower()
    is_course = bool(COURSE_HINTS.search(folder) or COURSE_HINTS.search(title or ""))
    umbrella = title_l in {"курсы", "courses", "не уроки", "гайды", "guides"}
    size = playlist_sizes.get(folder, 10**9)
    ratio = numbered_ratio.get(folder, 0.0)
    # Lower tuple = higher priority
    return (
        0 if is_course and not umbrella else 1 if is_course else 2,
        0 if ratio >= 0.5 else 1,
        0 if not umbrella else 1,
        size,  # smaller / more specific playlists first
        folder,
    )


def is_course_playlist(folder: str, title: str, video_names: list[str]) -> bool:
    if COURSE_HINTS.search(folder) or COURSE_HINTS.search(title or ""):
        return True
    if len(video_names) < 2:
        return False
    numbered = 0
    for name in video_names:
        if (
            LEADING_N_DOT.match(name)
            or LEADING_N_DOT_OK.match(name)
            or LEADING_HASH_N.match(name)
            or LEADING_HASH_OK.match(name)
            or LEADING_LESSON.match(name)
            or LEADING_LESSON_OK.match(name)
            or ALREADY_PREFIX.match(name)
        ):
            numbered += 1
    return numbered / len(video_names) >= 0.5


def hybrid_new_name(name: str, index: int) -> str:
    """Pad existing leading order markers, else add NN_ from playlist index."""
    if re.match(rf"^{index:02d}_", name):
        return name

    m = LEADING_N_DOT.match(name)
    if m:
        return f"0{m.group(1)}.{m.group(2)}" + name[m.end() :]

    m = LEADING_HASH_N.match(name)
    if m:
        return f"#0{m.group(1)}{m.group(2)}" + name[m.end() :]

    m = LEADING_LESSON.match(name)
    if m:
        return f"{m.group(1)} 0{m.group(2)}{m.group(3)}" + name[m.end() :]

    if (
        LEADING_N_DOT_OK.match(name)
        or LEADING_HASH_OK.match(name)
        or LEADING_LESSON_OK.match(name)
        or ALREADY_PREFIX.match(name)
    ):
        return name

    return f"{index:02d}_" + name


def apply_renames(plans: list[tuple[Path, Path]], dry_run: bool) -> None:
    if dry_run:
        for src, dst in plans:
            print(f"  [dry-run] RENAME {src.name} -> {dst.name}")
        return

    temp: list[tuple[Path, Path]] = []
    for i, (src, dst) in enumerate(plans):
        tmp = src.parent / f"__tmp_order_{i:04d}__{src.suffix}"
        src.rename(tmp)
        temp.append((tmp, dst))

    for tmp, dst in temp:
        if dst.exists():
            raise FileExistsError(f"Target already exists: {dst.name}")
        tmp.rename(dst)


def organize(args: argparse.Namespace) -> int:
    root = collection_root(args)
    if not root.is_dir():
        output_base = (
            args.output_dir
            if args.output_dir is not None
            else args.workspace / DEFAULT_OUTPUT_DIRNAME
        )
        raise SystemExit(
            f"Folder not found: {root}\n"
            f"Expected a collection directory under {output_base.resolve()}/"
        )

    local_files = list_local_videos(root)
    print(f"Target: {root}")
    print(f"Local videos in root: {len(local_files)}")
    if not local_files:
        raise SystemExit(
            "No videos found in the folder root. "
            "Expected files like 'Title [xxxxxxxxxxx].mp4'."
        )

    playlists_url = detect_channel(args, local_files)
    print(f"Channel playlists URL: {playlists_url}")

    print("Fetching channel playlists...")
    playlists_meta = yt_dlp_flat_json(args, playlists_url)
    if not playlists_meta:
        raise SystemExit("No playlists found for this channel.")

    video_to_playlists: dict[str, list[tuple[str, str, int]]] = {}
    playlist_info: dict[str, dict] = {}
    used_folders: set[str] = set()

    for pl in playlists_meta:
        pl_id = pl.get("id") or ""
        pl_title = pl.get("title") or "unnamed"
        pl_url = pl.get("url") or f"https://www.youtube.com/playlist?list={pl_id}"
        folder_name = unique_folder_name(slugify_playlist_name(pl_title), used_folders)
        print(f"  Fetching: {pl_title} -> {folder_name}")

        try:
            videos = yt_dlp_flat_json(args, pl_url)
        except RuntimeError as exc:
            print(f"    WARN: skip playlist ({exc})")
            continue

        playlist_info[folder_name] = {
            "title": pl_title,
            "id": pl_id,
            "folder": folder_name,
            "url": pl_url,
            "videos": [],
        }

        for entry in videos:
            vid = entry.get("id")
            if not vid:
                continue
            idx = entry.get("playlist_index") or entry.get("playlist_autonumber") or 0
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                idx = 0
            playlist_info[folder_name]["videos"].append(
                {"id": vid, "title": entry.get("title", ""), "index": idx}
            )
            video_to_playlists.setdefault(vid, []).append((folder_name, pl_title, idx))

    playlist_sizes = {
        folder: len(info["videos"]) for folder, info in playlist_info.items()
    }
    numbered_ratio: dict[str, float] = {}
    for folder, info in playlist_info.items():
        titles = [v.get("title") or "" for v in info["videos"]]
        if not titles:
            numbered_ratio[folder] = 0.0
            continue
        n = sum(
            1
            for t in titles
            if LEADING_N_DOT.match(t)
            or LEADING_N_DOT_OK.match(t)
            or LEADING_HASH_N.match(t)
            or LEADING_LESSON.match(t)
            or LEADING_LESSON_OK.match(t)
        )
        numbered_ratio[folder] = n / len(titles)

    moves: dict[Path, str] = {}
    misc: list[Path] = []
    for vid, filepath in local_files.items():
        if vid in video_to_playlists:
            candidates = sorted(
                video_to_playlists[vid],
                key=lambda item: playlist_priority(
                    item, playlist_sizes, numbered_ratio
                ),
            )
            moves[filepath] = candidates[0][0]
        else:
            misc.append(filepath)

    report_moved: dict[str, list[str]] = {}
    for filepath, folder in sorted(moves.items(), key=lambda x: (x[1], x[0].name)):
        dest = root / folder / filepath.name
        safe_move(filepath, dest, args.dry_run)
        report_moved.setdefault(folder, []).append(filepath.name)

    for filepath in misc:
        dest = root / "misc" / filepath.name
        safe_move(filepath, dest, args.dry_run)
        report_moved.setdefault("misc", []).append(filepath.name)

    # Build order map folder -> {video_id: index} from playlist_info
    order_log: dict[str, list[dict]] = {}
    if args.order_mode != "none":
        print("\nApplying playlist order renames...")
        for folder, info in playlist_info.items():
            folder_path = root / folder
            if not folder_path.is_dir() and not args.dry_run:
                continue

            # Collect current files (after move, or planned names on dry-run)
            if args.dry_run:
                names = report_moved.get(folder, [])
                files = [folder_path / n for n in names]
            else:
                if not folder_path.is_dir():
                    continue
                files = [
                    f
                    for pat in ("*.mp4", "*.mkv", "*.webm")
                    for f in folder_path.glob(pat)
                ]

            if len(files) < 2:
                continue

            title = info["title"]
            if args.order_mode == "courses" and not is_course_playlist(
                folder, title, [f.name for f in files]
            ):
                continue

            pl_order = {
                v["id"]: int(v["index"])
                for v in info["videos"]
                if v.get("id") and v.get("index")
            }
            plans: list[tuple[Path, Path]] = []
            print(f"  Ordering: {folder} ({title})")
            for f in sorted(files, key=lambda p: p.name.lower()):
                vid = extract_video_id(f.name)
                if not vid or vid not in pl_order:
                    print(f"    WARN: no playlist index for {f.name[:80]}")
                    continue
                new_name = hybrid_new_name(f.name, pl_order[vid])
                if new_name != f.name:
                    plans.append((f, f.parent / new_name))

            if plans:
                apply_renames(plans, args.dry_run)
                order_log[folder] = [{"from": a.name, "to": b.name} for a, b in plans]
                print(f"    {len(plans)} renames")
            else:
                print("    no renames needed")

    full_report = {
        "folder": str(root),
        "channel_playlists_url": playlists_url,
        "dry_run": args.dry_run,
        "order_mode": args.order_mode,
        "playlists": {
            k: {
                "title": v["title"],
                "id": v["id"],
                "folder": v["folder"],
                "video_count_in_playlist": len(v["videos"]),
                "local_moved": len(report_moved.get(k, [])),
            }
            for k, v in playlist_info.items()
        },
        "moved": report_moved,
        "order_renames": order_log,
        "stats": {
            "local_total": len(local_files),
            "moved_to_playlists": sum(
                len(v) for k, v in report_moved.items() if k != "misc"
            ),
            "moved_to_misc": len(report_moved.get("misc", [])),
            "renamed": sum(len(v) for v in order_log.values()),
        },
    }

    report_path = root / "_organize_report.json"
    if not args.dry_run:
        report_path.write_text(
            json.dumps(full_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if order_log:
            (root / "_order_renames.json").write_text(
                json.dumps(order_log, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    print("\n=== Summary ===")
    for folder, files in sorted(report_moved.items()):
        pl_title = playlist_info.get(folder, {}).get("title", "misc")
        print(f"  {folder}/ ({pl_title}): {len(files)} files")
    print(
        f"\nStats: playlists={full_report['stats']['moved_to_playlists']}, "
        f"misc={full_report['stats']['moved_to_misc']}, "
        f"renamed={full_report['stats']['renamed']}"
    )
    if not args.dry_run:
        print(f"Report: {report_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return organize(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
