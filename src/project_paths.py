"""Shared workspace paths for yt-dlp pipeline scripts."""

from __future__ import annotations

from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIRNAME = "output"
DEFAULT_CACHE_DIRNAME = "cache"


def output_dir(workspace: Path | None = None) -> Path:
    return (workspace or WORKSPACE_ROOT) / DEFAULT_OUTPUT_DIRNAME


def cache_dir(workspace: Path | None = None) -> Path:
    return (workspace or WORKSPACE_ROOT) / DEFAULT_CACHE_DIRNAME
