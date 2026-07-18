"""Map YouTube videos to a single best-matching playlist (shared with organize_by_playlists)."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

COURSE_HINTS = re.compile(
    r"курс|course|урок|lecture|tutorial|мастер.?класс|с\s*нул|"
    r"crash\s*course|full\s*course|roadmap|практик|урок",
    re.I,
)
LEADING_N_DOT = re.compile(r"^(\d)\.(\s)")
LEADING_N_DOT_OK = re.compile(r"^\d{2,}\.\s")
LEADING_HASH_N = re.compile(r"^#(\d)(\s)")
LEADING_LESSON = re.compile(
    r"^(Урок|Lesson|Part|Часть)\s+(\d)([\.\s:])", re.I
)
LEADING_LESSON_OK = re.compile(
    r"^(Урок|Lesson|Part|Часть)\s+\d{2,}([\.\s:])", re.I
)


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
        max_base = 60 - len(suffix)
        name = (base[:max_base] + suffix) if len(base) + len(suffix) > 60 else base + suffix
        n += 1
    used.add(name)
    return name


def playlist_priority(
    item: tuple[str, str, int],
    playlist_sizes: dict[str, int],
    numbered_ratio: dict[str, float],
) -> tuple:
    folder, title, _ = item
    title_l = (title or "").strip().lower()
    is_course = bool(COURSE_HINTS.search(folder) or COURSE_HINTS.search(title or ""))
    umbrella = title_l in {"курсы", "courses", "не уроки", "гайды", "guides"}
    size = playlist_sizes.get(folder, 10**9)
    ratio = numbered_ratio.get(folder, 0.0)
    return (
        0 if is_course and not umbrella else 1 if is_course else 2,
        0 if ratio >= 0.5 else 1,
        0 if not umbrella else 1,
        size,
        folder,
    )


def _numbered_ratio_for_playlist(video_titles: list[str]) -> float:
    if not video_titles:
        return 0.0
    numbered = sum(
        1
        for title in video_titles
        if LEADING_N_DOT.match(title)
        or LEADING_N_DOT_OK.match(title)
        or LEADING_HASH_N.match(title)
        or LEADING_LESSON.match(title)
        or LEADING_LESSON_OK.match(title)
    )
    return numbered / len(video_titles)


def yt_dlp_flat_json(run_yt_dlp: Callable[..., subprocess.CompletedProcess], url: str) -> list[dict]:
    proc = run_yt_dlp("--flat-playlist", "--dump-json", url)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"yt-dlp failed for {url}:\n{err[:2000]}")
    entries: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def build_video_playlist_map(
    channel_id: str,
    run_yt_dlp: Callable[..., subprocess.CompletedProcess],
) -> dict[str, str]:
    """Return video_id -> chosen playlist title (empty if none)."""
    playlists_url = f"https://www.youtube.com/channel/{channel_id}/playlists"
    playlists_meta = yt_dlp_flat_json(run_yt_dlp, playlists_url)
    if not playlists_meta:
        return {}

    video_to_playlists: dict[str, list[tuple[str, str, int]]] = {}
    playlist_info: dict[str, dict] = {}

    for pl in playlists_meta:
        pl_id = pl.get("id") or ""
        pl_title = pl.get("title") or "unnamed"
        pl_url = pl.get("url") or f"https://www.youtube.com/playlist?list={pl_id}"
        folder_name = slugify_playlist_name(pl_title)

        try:
            videos = yt_dlp_flat_json(run_yt_dlp, pl_url)
        except RuntimeError:
            continue

        titles = [entry.get("title") or "" for entry in videos]
        playlist_info[folder_name] = {
            "title": pl_title,
            "titles": titles,
            "size": len(videos),
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
            video_to_playlists.setdefault(vid, []).append((folder_name, pl_title, idx))

    playlist_sizes = {folder: info["size"] for folder, info in playlist_info.items()}
    numbered_ratio = {
        folder: _numbered_ratio_for_playlist(info["titles"])
        for folder, info in playlist_info.items()
    }

    result: dict[str, str] = {}
    for vid, candidates in video_to_playlists.items():
        best = sorted(
            candidates,
            key=lambda item: playlist_priority(item, playlist_sizes, numbered_ratio),
        )[0]
        result[vid] = best[1]
    return result
