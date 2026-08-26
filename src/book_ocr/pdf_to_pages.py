"""
Render the spreads of a book's input PDF into pages_extracted/ as PNG.

A book that arrived as one PDF starts the pipeline here instead of at
scan_pages.py: every PDF page is one scanned spread, and the OCR step then
reads the PNGs exactly as it reads screenshots captured from Archive.org.

Rendering is cached: a spread already present is left alone, so a run only
materialises what it is about to OCR. Scale is chosen so that one book page
(half a spread) lands near --page-long-side pixels, because the vision model
downsizes anything larger and a scan of this size gains nothing from more.

Usage (from the workspace root):
  python src\\book_ocr\\pdf_to_pages.py --book _tutorial_spanish --all
  python src\\book_ocr\\pdf_to_pages.py --book _tutorial_spanish --first 9 --last 20
  python src\\book_ocr\\pdf_to_pages.py --book _tutorial_spanish --slugs page_009
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from book_paths import DEFAULT_BOOK, img_dir, pdf_source

# A book page wider than this gains nothing: the vision API downsizes images
# whose long side exceeds ~1568 px, so rendering past that only costs upload.
DEFAULT_PAGE_LONG_SIDE = 1500
MIN_DPI = 72
MAX_DPI = 600
# A page noticeably wider than tall holds two book pages side by side.
SPREAD_ASPECT = 1.15


def slug_for_index(index: int) -> str:
    """PNG name for a zero-based PDF page index (page_001 = first PDF page)."""
    return f"page_{index + 1:03d}"


def index_for_slug(slug: str) -> int | None:
    stem = slug.strip()
    if not stem.startswith("page_"):
        return None
    tail = stem[len("page_") :]
    return int(tail) - 1 if tail.isdigit() else None


def open_pdf(pdf: Path):
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover - depends on the environment
        print(
            "PyMuPDF is required to render a PDF book.\n"
            "  pip install -r src/requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return fitz.open(pdf)


def render_dpi(page, page_long_side: int) -> float:
    """DPI at which one book page of this spread reaches page_long_side pixels."""
    rect = page.rect
    is_spread = rect.width > rect.height * SPREAD_ASPECT
    page_w = rect.width / 2 if is_spread else rect.width
    long_side_pt = max(page_w, rect.height)
    if long_side_pt <= 0:
        return 150.0
    return min(MAX_DPI, max(MIN_DPI, 72.0 * page_long_side / long_side_pt))


def render_spread(doc, index: int, page_long_side: int) -> bytes:
    page = doc[index]
    pix = page.get_pixmap(dpi=round(render_dpi(page, page_long_side)))
    return pix.tobytes("png")


def spread_slugs(doc) -> list[str]:
    return [slug_for_index(i) for i in range(doc.page_count)]


def ensure_spread(
    doc, index: int, dest_dir: Path, page_long_side: int, overwrite: bool = False
) -> Path:
    """Write the spread's PNG if it is not cached yet; return its path."""
    dest = dest_dir / f"{slug_for_index(index)}.png"
    if dest.exists() and not overwrite:
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(render_spread(doc, index, page_long_side))
    return dest


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Render input PDF spreads to pages_extracted/")
    p.add_argument("--book", default=DEFAULT_BOOK, help="Book folder name under _books/")
    p.add_argument("--pdf", type=Path, default=None, help="Explicit PDF (default: the one in input/)")
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--all", action="store_true", help="Render every spread")
    p.add_argument("--first", type=int, default=None, help="First PDF page, 1-based")
    p.add_argument("--last", type=int, default=None, help="Last PDF page, 1-based")
    p.add_argument("--slugs", nargs="+", default=None, help="Render only these (page_009 ...)")
    p.add_argument("--page-long-side", type=int, default=DEFAULT_PAGE_LONG_SIDE)
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args()
    if args.pdf is None:
        args.pdf = pdf_source(args.book)
    if args.out_dir is None:
        args.out_dir = img_dir(args.book)
    return args


def main() -> int:
    args = parse_args()
    if args.pdf is None or not args.pdf.exists():
        print(
            f"No input PDF for book {args.book!r}: expected one in "
            f"_books/{args.book}/input/",
            file=sys.stderr,
        )
        return 2

    doc = open_pdf(args.pdf)
    total = doc.page_count
    print(f"PDF: {args.pdf.name} ({total} spreads)", flush=True)

    if args.slugs:
        indexes = []
        for slug in args.slugs:
            idx = index_for_slug(slug)
            if idx is None or not 0 <= idx < total:
                print(f"Unknown or out-of-range slug: {slug}", file=sys.stderr)
                return 2
            indexes.append(idx)
    elif args.all or args.first or args.last:
        first = (args.first or 1) - 1
        last = (args.last or total) - 1
        if not (0 <= first <= last < total):
            print(f"Bad page range: {args.first}..{args.last} of {total}", file=sys.stderr)
            return 2
        indexes = list(range(first, last + 1))
    else:
        print("Nothing to do: pass --all, --first/--last or --slugs.", file=sys.stderr)
        return 2

    written = skipped = 0
    for idx in indexes:
        dest = args.out_dir / f"{slug_for_index(idx)}.png"
        if dest.exists() and not args.overwrite:
            skipped += 1
            continue
        ensure_spread(doc, idx, args.out_dir, args.page_long_side, overwrite=True)
        written += 1
        print(f"  rendered {dest.name}", flush=True)

    print(
        f"Done. rendered={written}, already present={skipped}, out={args.out_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
