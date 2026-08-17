"""Map YouTube videos to a single best-matching playlist (shared with organize_by_playlists)."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

from shared.project_paths import spell_out_tech_names

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


def slugify_playlist_name(
    title: str, max_len: int = 60, context: str = ""
) -> str:
    """Folder name for a playlist, transliterated and stripped of marks.

    "context" is what the rest of the channel is about - its other
    playlist titles, its name - and settles the one name whose meaning
    the stripping would change: C# (see spell_out_tech_names).
    """
    return _slug(spell_out_tech_names(title, context), max_len)


def former_folder_name(title: str, max_len: int = 60) -> str:
    """The folder this title used to be given, before C# was spelled out.

    A channel synced under the old rule keeps its downloads in
    "advanced_c"; knowing that name lets the folder be renamed rather
    than left behind next to an empty "advanced_c_sharp".
    """
    return _slug(title, max_len)


def _slug(title: str, max_len: int) -> str:
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


def channel_context(playlists_meta: list[dict]) -> str:
    """What the channel is about, in its own words.

    Every playlist title plus the channel name yt-dlp writes on them:
    enough to tell a .NET channel from a piano one when a playlist is
    called "Advanced C#".
    """
    titles = [str(pl.get("title") or "") for pl in playlists_meta]
    names = {
        str(pl.get("channel") or pl.get("uploader") or "")
        for pl in playlists_meta
    }
    return " ".join(titles + sorted(name for name in names if name))


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


# A channel that never made a playlist has no playlists tab at all, and
# yt-dlp reports the missing tab as an error.
NO_PLAYLISTS_TAB = re.compile(r"does not have a playlists tab", re.I)


def channel_playlists_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}/playlists"


def no_playlists_tab(error: object) -> bool:
    """Whether this yt-dlp failure means "this channel keeps no playlists"."""
    return bool(NO_PLAYLISTS_TAB.search(str(error)))


def channel_playlists_json(
    run_yt_dlp: Callable[..., subprocess.CompletedProcess],
    channel_id: str,
) -> list[dict]:
    """Every playlist of a channel - and none is an answer, not a failure.

    A channel can simply have videos and nothing else; then all of them
    belong to the misc folder, and the rest of the pipeline works from
    an empty playlist list.
    """
    try:
        return yt_dlp_flat_json(run_yt_dlp, channel_playlists_url(channel_id))
    except RuntimeError as exc:
        if no_playlists_tab(exc):
            return []
        raise


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


def seconds_to_duration_text(seconds: object) -> str | None:
    """Convert yt-dlp duration (seconds) to the browse-cache text form."""
    try:
        total = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_video_playlist_catalog(
    channel_id: str,
    run_yt_dlp: Callable[..., subprocess.CompletedProcess],
) -> tuple[dict[str, str], dict[str, dict]]:
    """Return (video_id -> playlist title, video_id -> detail fields).

    Details cover every video seen in any channel playlist (title, duration,
    url, index in the winning playlist) so summaries can also list videos that
    appear only in playlists and not in the channel Videos/uploads feed.
    """
    playlists_meta = channel_playlists_json(run_yt_dlp, channel_id)
    if not playlists_meta:
        return {}, {}

    video_to_playlists: dict[str, list[tuple[str, str, int]]] = {}
    playlist_info: dict[str, dict] = {}
    details: dict[str, dict] = {}
    context = channel_context(playlists_meta)

    for pl in playlists_meta:
        pl_id = pl.get("id") or ""
        pl_title = pl.get("title") or "unnamed"
        pl_url = pl.get("url") or f"https://www.youtube.com/playlist?list={pl_id}"
        folder_name = slugify_playlist_name(pl_title, context=context)

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
            prev = details.get(vid) or {}
            details[vid] = {
                "title": entry.get("title") or prev.get("title") or vid,
                "duration_text": seconds_to_duration_text(entry.get("duration"))
                or prev.get("duration_text"),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "playlist_index": idx,
            }

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
        if vid in details:
            details[vid]["playlist_index"] = best[2]
    return result, details


def build_video_playlist_map(
    channel_id: str,
    run_yt_dlp: Callable[..., subprocess.CompletedProcess],
) -> dict[str, str]:
    """Return video_id -> chosen playlist title (empty if none)."""
    mapping, _details = build_video_playlist_catalog(channel_id, run_yt_dlp)
    return mapping


def playlist_only_browse_entries(
    playlist_map: dict[str, str],
    details: dict[str, dict],
    known_ids: set[str],
) -> list[dict]:
    """Browse-shaped entries for playlist videos missing from channel uploads.

    Ordered by (playlist title, playlist_index, video id) so bypls sections
    stay stable when the extras are appended to the uploads list.
    """
    extras: list[tuple[str, int, str, dict]] = []
    for vid, pl_title in playlist_map.items():
        if vid in known_ids:
            continue
        meta = details.get(vid) or {}
        title = (meta.get("title") or vid).strip()
        try:
            index = int(meta.get("playlist_index") or 0)
        except (TypeError, ValueError):
            index = 0
        extras.append(
            (
                pl_title or "",
                index,
                vid,
                {
                    "id": vid,
                    "title": title,
                    "duration_text": meta.get("duration_text"),
                    "relative_published": None,
                    "url": meta.get("url")
                    or f"https://www.youtube.com/watch?v={vid}",
                    "playlist_only": True,
                },
            )
        )
    extras.sort(key=lambda item: (item[0].lower(), item[1], item[2]))
    return [item[3] for item in extras]
