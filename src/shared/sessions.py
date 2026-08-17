#!/usr/bin/env python3
"""Running a batch of videos from the console: how many, and when to stop.

Every stage that works through a playlist takes --next, prints the same kind
of progress and lets 'p' end the run after the current video.
"""

from __future__ import annotations

import subprocess


def run_tool(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def normalize_next_count(value: str) -> int | None:
    """Session size; None means "every pending video"."""
    text = str(value).strip().lower()
    if text == "all":
        return None
    try:
        count = int(text)
    except ValueError:
        count = 0
    if count < 1:
        raise SystemExit(
            f"--next must be a positive number or 'all', got: {value!r}"
        )
    return count


def next_label(count: int | None) -> str:
    return "all" if count is None else str(count)


class PauseWatcher:
    """Soft pause: 'p' pressed in the console stops after the current video."""

    def __init__(self) -> None:
        try:
            import msvcrt

            self._msvcrt = msvcrt
        except ImportError:
            self._msvcrt = None

    def pause_requested(self) -> bool:
        if not self._msvcrt:
            return False
        requested = False
        while self._msvcrt.kbhit():
            if self._msvcrt.getwch().lower() == "p":
                requested = True
        return requested
