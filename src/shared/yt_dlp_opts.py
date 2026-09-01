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


# yt-dlp 2026+ needs the EJS solver distribution for some of YouTube's
# JavaScript challenges. Deno runs it locally; GitHub is yt-dlp's recommended
# source for the signed solver components.
YOUTUBE_REMOTE_COMPONENTS = ["--remote-components", "ejs:github"]


# Use the Windows certificate store instead of certifi's Mozilla bundle.
# Avast Web Shield intercepts HTTPS and re-signs every certificate with its
# own root, which lives in the Windows store but not in certifi's bundle -
# with certifi (yt-dlp's default when installed) every YouTube request dies
# with CERTIFICATE_VERIFY_FAILED.
YOUTUBE_SYSTEM_CERTS = ["--compat-options", "no-certifi"]


# Smallest stream good enough for ffmpeg to extract audio. Prefer a dedicated
# audio track; when cookies force the web client and YouTube serves only
# combined streams, take progressive HTTPS up to 360p instead of HLS 1080p.
TRANSCRIBE_DOWNLOAD_FORMAT = (
    "bestaudio[ext=m4a]/bestaudio/"
    "best[height<=360][protocol^=http]/best[height<=360]/best"
)


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
    # The Android client can bypass some anonymous-media 403 responses, but
    # yt-dlp cannot use it together with account cookies. In authenticated
    # mode leave client selection to yt-dlp so its web clients can use them.
    player_client = [] if cookies or browser else YOUTUBE_PLAYER_CLIENT
    return [
        *YOUTUBE_SYSTEM_CERTS,
        *YOUTUBE_REMOTE_COMPONENTS,
        *player_client,
        *cookie_args(cookies, browser),
    ]
