"""Shared paths for book translation (src/book_translate)."""

from __future__ import annotations

from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BOOKS_DIRNAME = "_books"
DEFAULT_BOOK = "Wojna_Futbolowa"
OUTPUT_DIRNAME = "OUTPUT"


def book_dir(book: str = DEFAULT_BOOK) -> Path:
    return WORKSPACE_ROOT / BOOKS_DIRNAME / book


def output_dir(book: str = DEFAULT_BOOK) -> Path:
    return book_dir(book) / OUTPUT_DIRNAME


def run_scripts_dir(book: str = DEFAULT_BOOK) -> Path:
    return book_dir(book) / "_run_scripts"
