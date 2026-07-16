#!/usr/bin/env python3
"""Organize downloaded videos into playlist folders based on YouTube channel playlists."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
CHANNEL_PLAYLISTS_URL = "https://www.youtube.com/channel/UCmjFlNOWMGP5VB0Q2RARaVA/playlists"
VIDEO_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.mp4$")

# Short folder names (Windows MAX_PATH safety for long video filenames)
SHORT_FOLDER_NAMES = {
    "PL0MUAHwery4ohqzclwSt4Q41ttek3UvQF": "foodieland_jsx_minista_vite",
    "PL0MUAHwery4p8yrb4eHKEjUuHgNxTqvK3": "kod_revyu",
    "PL0MUAHwery4omH4GyVQ-lI2R326tOdN7A": "react_kurs_2025",
    "PL0MUAHwery4qb4bilAQ9Is2NhgUsAmzkR": "vyorstka_s_nulya_html_scss_js",
    "PL0MUAHwery4pbHobHR5NGOMOxzELaLkAg": "html",
    "PL0MUAHwery4rqkzKF1mDBCIH_eZgjY6uN": "adaptivnaya_verstka_html_css_figma",
    "PL0MUAHwery4qHqXDmIadGQV8-TAgmzEkF": "frontend_tricks_tips",
    "PL0MUAHwery4o9I7QQVj_RP4ZVpmdx6evz": "css_kurs_2025",
    "PL0MUAHwery4qW_mKistLNWlh5ss1tstNi": "devtools",
    "PL0MUAHwery4ot0KmgGxlBSB7rXssLeA6h": "html_kurs_2025",
}


def slugify_playlist_name(title: str) -> str:
    """Convert playlist title to a Python-friendly folder name."""
    cyr_to_lat = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    title = title.lower()
    result = []
    for ch in title:
        if ch in cyr_to_lat:
            result.append(cyr_to_lat[ch])
        elif ch.isalnum():
            result.append(ch)
        else:
            result.append("_")
    slug = "".join(result)
    slug = re.sub(r"_+", "_", slug).strip("_")
    if len(slug) > 60:
        slug = slug[:60].rstrip("_")
    return slug or "unnamed_playlist"


def folder_name_for_playlist(pl_id: str, pl_title: str) -> str:
    return SHORT_FOLDER_NAMES.get(pl_id) or slugify_playlist_name(pl_title)


def yt_dlp_flat_json(url: str) -> list[dict]:
    proc = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-json", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    entries = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def extract_video_id(filename: str) -> str | None:
    m = VIDEO_ID_RE.search(filename)
    return m.group(1) if m else None


def safe_move(src: Path, dest: Path) -> None:
    """Move file, using extended path prefix on Windows if needed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_s, dest_s = str(src), str(dest)
    if sys.platform == "win32":
        if not src_s.startswith("\\\\?\\"):
            src_s = "\\\\?\\" + src_s
        if not dest_s.startswith("\\\\?\\"):
            dest_s = "\\\\?\\" + dest_s
    shutil.move(src_s, dest_s)


def main() -> None:
    print("Fetching channel playlists...")
    playlists_meta = yt_dlp_flat_json(CHANNEL_PLAYLISTS_URL)

    video_to_playlists: dict[str, list[tuple[str, str, int]]] = {}
    playlist_info: dict[str, dict] = {}

    for pl in playlists_meta:
        pl_id = pl["id"]
        pl_title = pl["title"]
        folder_name = folder_name_for_playlist(pl_id, pl_title)
        pl_url = pl["url"]
        print(f"  Fetching playlist: {pl_title} -> {folder_name}")

        videos = yt_dlp_flat_json(pl_url)
        playlist_info[folder_name] = {
            "title": pl_title,
            "id": pl_id,
            "folder": folder_name,
            "videos": [],
        }

        for entry in videos:
            vid = entry["id"]
            idx = entry.get("playlist_index") or entry.get("playlist_autonumber") or 0
            playlist_info[folder_name]["videos"].append(
                {"id": vid, "title": entry.get("title", ""), "index": idx}
            )
            video_to_playlists.setdefault(vid, []).append((folder_name, pl_title, idx))

    local_files: dict[str, Path] = {}
    for f in ROOT.glob("*.mp4"):
        vid = extract_video_id(f.name)
        if vid:
            local_files[vid] = f

    print(f"\nLocal videos found: {len(local_files)}")

    def playlist_priority(item: tuple[str, str, int]) -> tuple:
        folder, title, _ = item
        is_course = "kurs" in folder or bool(re.search(r"\d", title[:5] if title else ""))
        return (0 if is_course else 1, folder)

    moves: dict[Path, str] = {}
    misc: list[Path] = []

    for vid, filepath in local_files.items():
        if vid in video_to_playlists:
            candidates = sorted(video_to_playlists[vid], key=playlist_priority)
            moves[filepath] = candidates[0][0]
        else:
            misc.append(filepath)

    folders_created = set(moves.values()) | ({"misc"} if misc else set())
    for folder in folders_created:
        (ROOT / folder).mkdir(exist_ok=True)

    report: dict[str, list[str]] = {}
    for filepath, folder in sorted(moves.items(), key=lambda x: (x[1], x[0].name)):
        dest = ROOT / folder / filepath.name
        if dest.exists():
            print(f"  SKIP (exists): {dest.name}")
        elif filepath.exists():
            safe_move(filepath, dest)
        report.setdefault(folder, []).append(filepath.name)

    for filepath in misc:
        dest = ROOT / "misc" / filepath.name
        if dest.exists():
            print(f"  SKIP (exists): {dest.name}")
        elif filepath.exists():
            safe_move(filepath, dest)
        report.setdefault("misc", []).append(filepath.name)

    report_path = ROOT / "_organize_report.json"
    full_report = {
        "playlists": {
            k: {
                "title": v["title"],
                "folder": v["folder"],
                "video_count_in_playlist": len(v["videos"]),
                "local_moved": len(report.get(k, [])),
            }
            for k, v in playlist_info.items()
        },
        "moved": report,
        "stats": {
            "local_total": len(local_files),
            "moved_to_playlists": sum(len(v) for k, v in report.items() if k != "misc"),
            "moved_to_misc": len(report.get("misc", [])),
        },
    }
    report_path.write_text(json.dumps(full_report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Summary ===")
    for folder, files in sorted(report.items()):
        pl_title = playlist_info.get(folder, {}).get("title", "misc")
        print(f"  {folder}/ ({pl_title}): {len(files)} files")
    print(f"\nReport saved to: {report_path.name}")


if __name__ == "__main__":
    main()
