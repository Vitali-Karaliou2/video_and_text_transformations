"""Shared yt-dlp options used across download and remote transcription."""

from __future__ import annotations

from pathlib import Path


def cookie_args(cookies: Path | None, browser: str | None) -> list[str]:
    """yt-dlp cookie options; YouTube asks for them when it suspects a bot."""
    if cookies:
        return ["--cookies", str(cookies)]
    if browser:
        return ["--cookies-from-browser", browser]
    return []


# Metadata can still load while the default web client gets HTTP 403 on
# the media URL itself (common for unlisted videos). The android client
# keeps serving a downloadable stream without cookies.
YOUTUBE_PLAYER_CLIENT = [
    "--extractor-args",
    "youtube:player_client=android",
]


def youtube_media_args(
    cookies: Path | None = None, browser: str | None = None
) -> list[str]:
    """yt-dlp options needed to actually download YouTube media."""
    return [*YOUTUBE_PLAYER_CLIENT, *cookie_args(cookies, browser)]
