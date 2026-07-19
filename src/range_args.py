"""Resolve --from, --to, and --next range arguments."""

from __future__ import annotations

import argparse
import re
import sys

from video_cache import list_pos_to_display_number

NUMERIC_RE = re.compile(r"^-?\d+$")


def scan_range_end_source(argv: list[str] | None) -> str | None:
    """Return 'to' or 'next' for the last explicit range-end flag on the command line."""
    if argv is None:
        argv = sys.argv[1:]
    source: str | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"--to", "--next"}:
            source = "to" if token == "--to" else "next"
            index += 2 if index + 1 < len(argv) and not argv[index + 1].startswith("-") else 1
            continue
        if token.startswith("--to="):
            source = "to"
        elif token.startswith("--next="):
            source = "next"
        index += 1
    return source


def try_parse_int(raw: str) -> int | None:
    if NUMERIC_RE.fullmatch(raw.strip()):
        return int(raw.strip())
    return None


def match_videos_by_title(
    videos: list[dict],
    length_curr: int,
    length_old: int,
    query: str,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    query_lower = query.lower()
    exact: list[tuple[int, str]] = []
    prefix: list[tuple[int, str]] = []
    for pos, video in enumerate(videos):
        title = video.get("title", "").strip()
        if not title:
            continue
        display_number = list_pos_to_display_number(pos, length_curr, length_old)
        title_lower = title.lower()
        if title_lower == query_lower:
            exact.append((display_number, title))
        elif title_lower.startswith(query_lower):
            prefix.append((display_number, title))
    return exact, prefix


def resolve_title_endpoint(
    videos: list[dict],
    length_curr: int,
    length_old: int,
    query: str,
    flag: str,
) -> tuple[int | None, str | None]:
    if len(query) < 3:
        return None, (
            f"--{flag}: title search requires a string of at least 3 characters "
            f"({query!r} is too short)."
        )
    exact, prefix = match_videos_by_title(videos, length_curr, length_old, query)
    if len(exact) == 1:
        return exact[0][0], None
    if len(exact) > 1:
        return None, f"--{flag}: ambiguous exact title match for {query!r}."
    if len(prefix) == 1:
        return prefix[0][0], None
    if not prefix:
        return None, f"--{flag}: no video title matches {query!r}."
    titles = "; ".join(title[:80] for _, title in prefix[:5])
    return None, f"--{flag}: ambiguous title prefix {query!r}; matches include: {titles}."


def resolve_endpoint(
    raw: str,
    flag: str,
    videos: list[dict],
    length_curr: int,
    length_old: int,
) -> tuple[int | None, str | None]:
    value = raw.strip()
    numeric = try_parse_int(value)
    if numeric is not None:
        return numeric, None
    return resolve_title_endpoint(videos, length_curr, length_old, value, flag)


def resolve_range_args(
    args: argparse.Namespace,
    videos: list[dict],
    length_curr: int,
    length_old: int,
    *,
    default_from: int,
    default_to: int,
    argv: list[str] | None = None,
) -> tuple[int, int, bool, bool, str | None, str | None]:
    """Return (from, to, from_explicit, to_explicit, error, range_end_source)."""
    end_source = scan_range_end_source(argv)
    from_explicit = args.from_raw.strip() != str(default_from)
    to_explicit = args.to_raw.strip() != str(default_to)
    next_explicit = args.next_count is not None

    from_index, from_error = resolve_endpoint(
        args.from_raw,
        "from",
        videos,
        length_curr,
        length_old,
    )
    if from_error:
        return default_from, default_to, from_explicit, False, from_error, None

    if end_source == "next":
        if args.next_count is None:
            return (
                from_index or default_from,
                default_to,
                from_explicit,
                False,
                "--next requires a positive integer count.",
                None,
            )
        if args.next_count < 1:
            return (
                from_index or default_from,
                default_to,
                from_explicit,
                True,
                "--next count must be at least 1.",
                None,
            )
        start = from_index if from_index is not None else default_from
        to_index = start + args.next_count - 1
        return start, to_index, from_explicit, True, None, "next"

    if end_source == "to":
        to_index, to_error = resolve_endpoint(
            args.to_raw,
            "to",
            videos,
            length_curr,
            length_old,
        )
        if to_error:
            return (
                from_index or default_from,
                default_to,
                from_explicit,
                to_explicit,
                to_error,
                None,
            )
        start = from_index if from_index is not None else default_from
        end = to_index if to_index is not None else default_to
        return start, end, from_explicit, to_explicit, None, "to"

    if next_explicit and not to_explicit:
        if args.next_count is None or args.next_count < 1:
            return (
                from_index or default_from,
                default_to,
                from_explicit,
                False,
                "--next count must be at least 1.",
                None,
            )
        start = from_index if from_index is not None else default_from
        return start, start + args.next_count - 1, from_explicit, True, None, "next"

    to_index, to_error = resolve_endpoint(
        args.to_raw,
        "to",
        videos,
        length_curr,
        length_old,
    )
    if to_error:
        return (
            from_index or default_from,
            default_to,
            from_explicit,
            to_explicit,
            to_error,
            None,
        )
    start = from_index if from_index is not None else default_from
    end = to_index if to_index is not None else default_to
    return start, end, from_explicit, to_explicit, None, end_source


def apply_resolved_range(
    args: argparse.Namespace,
    from_index: int,
    to_index: int,
    *,
    from_explicit: bool,
    to_explicit: bool,
    range_end_source: str | None = None,
) -> None:
    args.from_index = from_index
    args.to_index = to_index
    args.from_explicit = from_explicit
    args.to_explicit = to_explicit
    args.range_end_source = range_end_source


def numeric_required_to(
    args: argparse.Namespace,
    *,
    default_from: int,
    default_to: int,
    argv: list[str] | None = None,
) -> int | None:
    """Best-effort upper fetch bound before title resolution (numeric args only)."""
    end_source = scan_range_end_source(argv)
    from_num = try_parse_int(args.from_raw.strip())
    if end_source == "next" and args.next_count is not None and from_num is not None:
        if args.from_raw.strip() == str(default_from) and args.next_count:
            return min(default_to, from_num + args.next_count - 1)
        return from_num + args.next_count - 1
    if end_source == "to" or (end_source is None and args.to_raw.strip() != str(default_to)):
        to_num = try_parse_int(args.to_raw.strip())
        if to_num is not None:
            return to_num
    if args.next_count is not None and from_num is not None:
        return from_num + args.next_count - 1
    if from_num is not None and from_num != default_from:
        return default_to
    return None
