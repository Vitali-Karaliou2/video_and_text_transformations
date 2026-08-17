#!/usr/bin/env python3
"""The OpenAI key, from the environment or from the .env of the workspace."""

from __future__ import annotations

import os
from pathlib import Path


def read_api_key(workspace: Path) -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    env_path = workspace / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("OPENAI_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    return key
    raise SystemExit(
        "OPENAI_API_KEY not found: set the environment variable or add\n"
        f"OPENAI_API_KEY=sk-... to {env_path}"
    )
