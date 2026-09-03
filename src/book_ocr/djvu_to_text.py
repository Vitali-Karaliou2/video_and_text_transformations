"""
Extract the embedded text layer of a DjVu book into pages_text/, one .md per
book page.

A DjVu that already carries an OCR text layer (this is how scanned Russian
technical books usually circulate) does not need the vision-model OCR step at
all: djvutxt reads the layer for free and the result lands in pages_text/ in
the same shape ocr_claude.py would produce, so everything downstream of
pages_text/ works unchanged. Pages are kept as printed - line breaks and
hyphenation included - because a faithful copy is the safest base for later
editing; nothing is reflowed.

Each run converts the next --pages pages (a page whose .md already exists is
skipped), so repeated runs simply advance through the book. Requires the
DjVuLibre command-line tools (ddjvu/djvutxt), installed e.g. via
    winget install DjVuLibre.DjView

Usage (from the workspace root):
  python src\\book_ocr\\djvu_to_text.py --settings _books\\<Book>\\_run_scripts\\convert_next_pages.settings.txt
  python src\\book_ocr\\djvu_to_text.py --book Artificial_Intelligence_2006 --pages 10
  python src\\book_ocr\\djvu_to_text.py --book Artificial_Intelligence_2006 --slugs page_0031 page_0032
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import book_paths

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

DJVULIBRE_DIRS = [
    r"C:\Program Files (x86)\DjVuLibre",
    r"C:\Program Files\DjVuLibre",
]


def find_djvutxt() -> str:
    exe = shutil.which("djvutxt")
    if exe:
        return exe
    for folder in DJVULIBRE_DIRS:
        candidate = Path(folder) / "djvutxt.exe"
        if candidate.is_file():
            return str(candidate)
    sys.exit(
        "ERROR: djvutxt not found. Install DjVuLibre, e.g.\n"
        "  winget install DjVuLibre.DjView"
    )


def djvu_source(book: str) -> Path:
    folder = book_paths.input_dir(book)
    files = sorted(folder.glob("*.djvu")) if folder.is_dir() else []
    if not files:
        sys.exit(f"ERROR: no .djvu file in {folder}")
    return files[0]


def page_count(djvutxt: str, djvu: Path) -> int:
    djvused = str(Path(djvutxt).with_name("djvused.exe"))
    out = subprocess.run(
        [djvused, str(djvu), "-e", "n"],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


def extract_page_text(djvutxt: str, djvu: Path, page: int) -> str:
    """Plain text of one DjVu page from its embedded OCR layer (UTF-8)."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            [djvutxt, f"--page={page}", str(djvu), str(tmp_path)],
            capture_output=True, text=True, check=True,
        )
        return tmp_path.read_text(encoding="utf-8")
    finally:
        tmp_path.unlink(missing_ok=True)


def tidy(text: str) -> str:
    """As printed, minus artefacts: trailing spaces, form feeds, >2 blank lines."""
    text = text.replace("\f", "\n")
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if not ln:
            blanks += 1
            if blanks > 2:
                continue
        else:
            blanks = 0
        out.append(ln)
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def slug_for(page: int, total: int) -> str:
    digits = max(3, len(str(total)))
    return f"page_{page:0{digits}d}"


def settings_defaults() -> dict[str, str]:
    return {"BOOK": "", "PAGES": "10"}


def load_settings(path: Path) -> dict[str, str]:
    settings = settings_defaults()
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if key in settings and key not in seen:
            settings[key] = value
            seen.add(key)
    return settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, help="settings file (UTF-8)")
    parser.add_argument("--book", default=None, help="book folder under _books\\")
    parser.add_argument("--pages", type=int, default=None,
                        help="how many next pages one run converts")
    parser.add_argument("--slugs", nargs="+", default=None,
                        help="convert exactly these pages (page_0031 ...), "
                             "overwriting existing .md")
    args = parser.parse_args()

    if args.settings:
        cfg = load_settings(args.settings)
        if args.book is None and cfg["BOOK"]:
            args.book = cfg["BOOK"]
        if args.pages is None:
            args.pages = int(cfg["PAGES"])
    if not args.book:
        sys.exit("ERROR: no book given (--book or BOOK in the settings file)")
    if args.pages is None:
        args.pages = 10
    return args


def main() -> None:
    args = parse_args()
    djvutxt = find_djvutxt()
    djvu = djvu_source(args.book)
    total = page_count(djvutxt, djvu)
    text_dir = book_paths.text_dir(args.book)
    text_dir.mkdir(parents=True, exist_ok=True)

    print(f"Book: {djvu.name} - {total} pages, text layer via djvutxt")

    if args.slugs:
        pages = []
        for slug in args.slugs:
            m = re.fullmatch(r"page_(\d+)", slug)
            if not m or not (1 <= int(m.group(1)) <= total):
                sys.exit(f"ERROR: bad slug {slug!r} (book has pages 1..{total})")
            pages.append(int(m.group(1)))
    else:
        done = {
            int(m.group(1))
            for f in text_dir.glob("page_*.md")
            if (m := re.fullmatch(r"page_(\d+)", f.stem))
        }
        pages = [p for p in range(1, total + 1) if p not in done][: args.pages]
        if not pages:
            print("Nothing to do: every page already has its .md")
            return
        print(f"Converting {len(pages)} pages: {pages[0]}..{pages[-1]} "
              f"({len(done)} of {total} were already done)")

    for page in pages:
        slug = slug_for(page, total)
        text = tidy(extract_page_text(djvutxt, djvu, page))
        body = text if text else "*(empty page - no text in the layer)*"
        md = (
            f"<!-- source: {djvu.name} page {page} of {total}; "
            f"embedded text layer, no OCR -->\n\n"
            f"## Page {page}\n\n{body}\n"
        )
        out = text_dir / f"{slug}.md"
        out.write_text(md, encoding="utf-8")
        chars = len(text)
        note = "  (empty)" if not text else ""
        print(f"  {slug}.md  {chars} chars{note}")

    print("Done.")


if __name__ == "__main__":
    main()
