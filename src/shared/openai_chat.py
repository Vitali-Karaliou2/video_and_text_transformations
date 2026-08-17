#!/usr/bin/env python3
"""One way of asking the chat model for a JSON answer, and what it costs.

Three stages talk to the same model - the slide reader, the editor and the
channel search - so the retry policy, the model name and its price live in
one place rather than in whichever of them was written first.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

CHAT_URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o"
CHAT_ATTEMPTS = 3
CHAT_TIMEOUT = 300
# gpt-4o API prices as of 2026-07, USD per 1M tokens (see
# https://platform.openai.com/docs/pricing).
USD_PER_MTOKEN_PROMPT = 2.50
USD_PER_MTOKEN_COMPLETION = 10.00


def call_chat_api(api_key: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(1, CHAT_ATTEMPTS + 1):
        request = urllib.request.Request(
            CHAT_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            retriable = exc.code in (429, 500, 502, 503, 504)
            if retriable and attempt < CHAT_ATTEMPTS:
                last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
                time.sleep(10 * attempt)
                continue
            raise SystemExit(f"OpenAI API error (HTTP {exc.code}):\n{detail}")
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < CHAT_ATTEMPTS:
                time.sleep(10 * attempt)
                continue
    raise SystemExit(f"OpenAI API request failed after retries: {last_error}")


def chat_json(
    api_key: str,
    messages: list[dict],
    usage: dict[str, int],
) -> dict:
    """Chat completion that must answer with a JSON object.

    One retry when what comes back is not JSON after all.
    """
    payload = {
        "model": MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    for attempt in (1, 2):
        data = call_chat_api(api_key, payload)
        for key in ("prompt_tokens", "completion_tokens"):
            usage[key] = usage.get(key, 0) + int(
                data.get("usage", {}).get(key, 0)
            )
        content = data["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            if attempt == 1:
                continue
            raise SystemExit(
                f"Model did not return valid JSON:\n{content[:2000]}"
            )
    raise AssertionError("unreachable")
