"""Shared paths for the book scanning/OCR module.

Book data lives under _books/<Book_Name>/ in the workspace root:
  input/                          <- the book as one file (PDF), when there is one
  pages.txt / pages_expanded.txt  <- Archive.org page URLs
  pages_extracted/                <- PNG images of 2-up spreads
  pages_text/                     <- OCR results as Markdown
  book/                           <- the document being assembled by hand

A book reaches pages_extracted/ one of two ways. One that had to be read off
Archive.org leaf by leaf gets there via scan_pages.py and needs the page URL
list; one that arrived as a single PDF gets there via pdf_to_pages.py and needs
no list, since the PDF itself enumerates the spreads. input/ therefore marks a
book whose pipeline starts from the file and takes precedence over the URL list.
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


def input_dir(book: str = DEFAULT_BOOK) -> Path:
    return book_dir(book) / "input"


def pdf_source(book: str = DEFAULT_BOOK) -> Path | None:
    """The book's PDF in input/, or None if the book has no such source."""
    folder = input_dir(book)
    if not folder.is_dir():
        return None
    pdfs = sorted(p for p in folder.glob("*.pdf") if p.is_file())
    return pdfs[0] if pdfs else None


def img_dir(book: str = DEFAULT_BOOK) -> Path:
    return book_dir(book) / "pages_extracted"


def text_dir(book: str = DEFAULT_BOOK) -> Path:
    return book_dir(book) / "pages_text"
