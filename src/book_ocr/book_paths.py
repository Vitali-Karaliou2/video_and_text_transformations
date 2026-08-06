"""Shared paths for the book scanning/OCR module.

Book data lives under _books/<Book_Name>/ in the workspace root:
  pages.txt / pages_expanded.txt  <- Archive.org page URLs
  pages_extracted/                <- PNG screenshots of 2-up spreads
  pages_text/                     <- OCR results as Markdown
  book/                           <- the document being assembled by hand
"""

from __future__ import annotations

from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BOOKS_DIRNAME = "_books"
DEFAULT_BOOK = "Wojna_Futbolowa"


def book_dir(book: str = DEFAULT_BOOK) -> Path:
    return WORKSPACE_ROOT / BOOKS_DIRNAME / book


def pages_file(book: str = DEFAULT_BOOK) -> Path:
    return book_dir(book) / "pages_expanded.txt"


def img_dir(book: str = DEFAULT_BOOK) -> Path:
    return book_dir(book) / "pages_extracted"


def text_dir(book: str = DEFAULT_BOOK) -> Path:
    return book_dir(book) / "pages_text"
