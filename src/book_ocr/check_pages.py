"""
Sanity checks for OCR results in _books/<book>/pages_text/.

Finds pages where part of the text was probably lost during extraction:

1. Short pages: non-blank printed pages whose extracted text is far below
   the median of full pages (story first/last pages are legitimately short,
   so this list is only a hint).
2. Broken continuity: a page ends mid-sentence (no terminal punctuation,
   no hyphen split) while the next non-blank page starts a new sentence
   with an uppercase letter. In a book whose text flows across pages this
   almost always means the tail of the earlier page was not extracted.

Usage (workspace root):
  python src\\book_ocr\\check_pages.py
  python src\\book_ocr\\check_pages.py --book Wojna_Futbolowa --short-threshold 0.7
"""

from __future__ import annotations

import argparse
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from book_paths import DEFAULT_BOOK, pages_file, text_dir

BLANK_MARKER = re.compile(
    r"blank|pusta|brak widoczn|brak tekstu|no readable|no visible|nieczyteln",
    re.I,
)
# End-of-page looks finished: sentence end, closing quote, or hyphen split.
ENDS_OK = re.compile(r"([.!?…»\"\u201d)\]]|-)$")
YEAR_END = re.compile(r"\d{4}\)?$")
STARTS_AS_CONTINUATION = re.compile(r"^[a-ząćęłńóśźż„«(\[]")


@dataclass
class PageUnit:
    slug: str
    heading: str
    text: str
    blank: bool


def slug_from_url(url: str) -> str:
    m = re.search(r"/page/([^/]+)/", url)
    if m:
        leaf = m.group(1)
        if leaf.isdigit():
            return f"page_{int(leaf):03d}"
        return f"page_{leaf}"
    return "page_000_cover"


def load_order(book: str) -> list[str]:
    slugs = []
    for raw in pages_file(book).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            slugs.append(slug_from_url(line))
    return slugs


def load_units(book: str) -> list[PageUnit]:
    units: list[PageUnit] = []
    root = text_dir(book)
    for slug in load_order(book):
        path = root / f"{slug}.md"
        if not path.exists():
            continue
        body = re.sub(r"(?m)^<!--.*?-->\s*", "", path.read_text(encoding="utf-8"))
        parts = re.split(r"(?m)^(## .+)$", body)
        i = 1
        while i < len(parts):
            heading = parts[i][3:].strip()
            content = parts[i + 1] if i + 1 < len(parts) else ""
            text = " ".join(ln.strip() for ln in content.splitlines() if ln.strip())
            blank = bool(BLANK_MARKER.search(text[:120])) or len(text) < 40
            units.append(PageUnit(slug, heading, text, blank))
            i += 2
    return units


def find_short_pages(units: list[PageUnit], threshold: float) -> list[tuple[PageUnit, float]]:
    full = [len(u.text) for u in units if not u.blank]
    med = statistics.median(full) if full else 0
    out = []
    for u in units:
        if not u.blank and med and len(u.text) < med * threshold:
            out.append((u, len(u.text) / med))
    return out


def find_breaks(units: list[PageUnit]) -> list[tuple[PageUnit, PageUnit]]:
    flowing = [u for u in units if not u.blank]
    breaks = []
    for prev, nxt in zip(flowing, flowing[1:]):
        end = prev.text.rstrip()
        ends_ok = bool(ENDS_OK.search(end)) or bool(YEAR_END.search(end))
        starts_cont = bool(STARTS_AS_CONTINUATION.match(nxt.text.lstrip()))
        if not ends_ok and not starts_cont:
            breaks.append((prev, nxt))
    return breaks


def main() -> int:
    ap = argparse.ArgumentParser(description="Check OCR pages for probable text loss")
    ap.add_argument("--book", default=DEFAULT_BOOK)
    ap.add_argument("--short-threshold", type=float, default=0.7)
    args = ap.parse_args()

    units = load_units(args.book)
    nonblank = [u for u in units if not u.blank]
    med = statistics.median([len(u.text) for u in nonblank]) if nonblank else 0
    print(f"page units: {len(units)} (non-blank {len(nonblank)}), median chars {med:.0f}")

    print("\n=== broken continuity (likely lost tail) ===")
    breaks = find_breaks(units)
    for prev, nxt in breaks:
        print(f"{prev.slug} [{prev.heading}] ends: …{prev.text[-70:]!r}")
        print(f"  next {nxt.slug} [{nxt.heading}] starts: {nxt.text[:70]!r}")
    print(f"total: {len(breaks)}")

    print("\n=== short non-blank pages (hint only) ===")
    for u, ratio in find_short_pages(units, args.short_threshold):
        print(f"{u.slug:22s} [{u.heading[:28]:30s}] {len(u.text):5d} chars ({ratio*100:.0f}%)")

    slugs = sorted({p.slug for p, _ in breaks})
    print(f"\nbroken-continuity slugs: {' '.join(slugs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
