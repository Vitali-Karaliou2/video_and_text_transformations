"""Transcription cost estimation based on OpenAI audio API rates.

The current per-minute rate is cached in <workspace>/_cache/transcription_pricing.json
(one shared cache for all channels, unlike the per-channel caches).
The cache is re-checked against the OpenAI pricing page at most once per
REFRESH_DAYS; if the page cannot be fetched or parsed, the cached value is kept.
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# whisper-1 / gpt-4o-transcribe rate as of 2026-07 ($0.36 per hour of audio).
DEFAULT_USD_PER_MINUTE = 0.006
REFRESH_DAYS = 30
PRICING_URL = "https://platform.openai.com/docs/pricing"
PRICING_MODEL = "whisper-1 / gpt-4o-transcribe"
_RATE_RE = re.compile(
    r"whisper-1.{0,300}?\$?\s*(0\.\d{3,4})\s*(?:/|per)\s*min",
    re.IGNORECASE | re.DOTALL,
)


def pricing_cache_path(workspace: Path) -> Path:
    return workspace / "_cache" / "transcription_pricing.json"


def _fetch_rate_from_web() -> float | None:
    try:
        req = urllib.request.Request(
            PRICING_URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    match = _RATE_RE.search(html)
    if not match:
        return None
    try:
        rate = float(match.group(1))
    except ValueError:
        return None
    return rate if 0 < rate < 1 else None


def get_transcription_rate(workspace: Path) -> float:
    """Return the cached USD-per-minute rate, refreshing it if stale."""
    path = pricing_cache_path(workspace)
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}

    rate = data.get("usd_per_minute")
    checked = data.get("checked") or data.get("updated")
    stale = True
    if checked:
        try:
            stale = date.today() - date.fromisoformat(checked) > timedelta(
                days=REFRESH_DAYS
            )
        except ValueError:
            stale = True

    if rate is None or stale:
        today = date.today().isoformat()
        fetched = _fetch_rate_from_web()
        if fetched is not None:
            rate = fetched
            data["usd_per_minute"] = fetched
            data["updated"] = today
        elif rate is None:
            rate = DEFAULT_USD_PER_MINUTE
            data["usd_per_minute"] = rate
            data.setdefault("updated", today)
        data["checked"] = today
        data.setdefault("model", PRICING_MODEL)
        data.setdefault("source", PRICING_URL)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return float(rate)


def transcription_passes(lang: str | None) -> int:
    """One pass in the original language plus an English pass for non-English."""
    normalized = (lang or "en").strip().lower()
    return 1 if normalized == "en" else 2


def estimate_cost_usd(
    duration_seconds: int | None, usd_per_minute: float, passes: int
) -> float | None:
    if duration_seconds is None:
        return None
    return duration_seconds / 60.0 * usd_per_minute * passes


def format_cost_usd(cost: float | None) -> str:
    return "$?" if cost is None else f"${cost:.2f}"
