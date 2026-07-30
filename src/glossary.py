"""Course glossary: the terms a lecture series keeps using.

The glossary is a plain text file - one term per line, '#' starts a comment -
kept next to the material it describes: <playlist>/terms.txt for one lecture
series, <channel>/terms.txt for a glossary shared by every playlist of the
channel. The --terms flag of a script overrides both.

It is used at two points of the pipeline, and neither costs a token:

- transcribe_videos passes the terms to whisper in the `prompt` field, which
  biases recognition towards the spelling of the product and technology names
  the speaker actually uses ("Playwright", not "PlevRite");
- check_transcripts treats the terms as known words, so a deliberate loanword
  is not reported as an anomaly.

Example file:

    # Test automation course
    Playwright
    Selenium WebDriver
    SDET
    CI/CD
"""

from __future__ import annotations

import re
from pathlib import Path

GLOSSARY_FILENAME = "terms.txt"
COMMENT_RE = re.compile(r"(?:^|\s)#")
# whisper reads at most 224 tokens of `prompt` and silently ignores the rest.
WHISPER_PROMPT_TOKENS = 224
# Cyrillic packs fewer characters per token than Latin text.
CHARS_PER_TOKEN_CYRILLIC = 2.5
CHARS_PER_TOKEN_LATIN = 4.0


def estimate_tokens(text: str) -> int:
    cyrillic = sum(1 for char in text if "\u0400" <= char <= "\u04ff")
    latin = len(text) - cyrillic
    return int(
        cyrillic / CHARS_PER_TOKEN_CYRILLIC + latin / CHARS_PER_TOKEN_LATIN
    ) + 1


def find_glossary(
    playlist_dir: Path | None = None,
    channel_dir: Path | None = None,
    explicit: Path | None = None,
) -> Path | None:
    """The glossary to use: --terms, then the playlist one, then the channel one."""
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"Glossary file not found: {path}")
        return path
    for folder in (playlist_dir, channel_dir):
        if folder is None:
            continue
        candidate = Path(folder) / GLOSSARY_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_terms(path: Path | None) -> list[str]:
    """Terms of the glossary file, in file order, without duplicates."""
    if path is None:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        # A comment starts the line or follows a space, so that a term may
        # carry a '#' of its own ("C#").
        comment = COMMENT_RE.search(line)
        term = (line[: comment.start()] if comment else line).strip()
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            terms.append(term)
    return terms


def whisper_prompt(terms: list[str]) -> str:
    """The `prompt` field for the audio API: as many terms as the limit allows.

    whisper reads the prompt as the text preceding the audio, so a plain
    enumeration is enough to prime the spelling.
    """
    if not terms:
        return ""
    kept: list[str] = []
    for term in terms:
        candidate = ", ".join([*kept, term]) + "."
        if estimate_tokens(candidate) > WHISPER_PROMPT_TOKENS:
            break
        kept.append(term)
    return ", ".join(kept) + "." if kept else ""


def term_words(terms: list[str]) -> set[str]:
    """Lower-case words of the glossary, for whitelisting in the checks."""
    words: set[str] = set()
    for term in terms:
        for word in "".join(
            char if char.isalnum() or char == "-" else " " for char in term
        ).split():
            if len(word) > 1:
                words.add(word.lower())
    return words
