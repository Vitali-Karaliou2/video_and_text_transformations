"""
Re-extraction pipeline for book pages whose scans lost content.

Preparation (manual): review scans in _books/<book>/pages_extracted/ and COPY
the defective ones into a subfolder of pages_extracted/, e.g.
  _books/Wojna_Futbolowa/pages_extracted/reextracted_2026_08_09/
The subfolder both archives the bad captures and defines WHICH spreads the
pipeline processes (slugs are taken from the *.png names inside it).

Pipeline steps, each enabled by its own flag (any subset, executed in order):
  --scan       re-capture those spreads from Archive.org into pages_extracted/
               (requires the debug Edge session with an active Borrow;
               see _run_scripts/book_start_edge_for_scan.ps1)
  --ocr        re-OCR those spreads (one request per page half) into pages_text/
  --assemble   rebuild OUTPUT/Book_<lang>.md/.docx/.pdf from all pages
  --translate  re-translate, in place, the already-translated sections affected
               by those spreads, in every working translation file
               (OUTPUT/Book_<TO>[_b]_<model>.md); untranslated sections are
               skipped — they will be translated by future portion runs

Usage (workspace root):
  python src\\book_ocr\\reextract_pages.py --folder reextracted_2026_08_09 --scan --ocr
  python src\\book_ocr\\reextract_pages.py --folder reextracted_2026_08_09 --assemble --translate --to RU

Adapting to another book: create _books/<Book>/ with the same layout, adjust
STORY_STARTS in assemble_book.py (section boundaries of that book), and pass
--book <Book>.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from book_paths import DEFAULT_BOOK, WORKSPACE_ROOT, book_dir, img_dir

# Section titles are Polish; the default Windows console codepage cannot
# encode them, which would abort the run on a plain print().
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SRC_OCR = Path("src") / "book_ocr"
SRC_TRANSLATE = Path("src") / "book_translate"

MODEL_TAG_TO_ALIAS = {"sonn": "sonnet", "opus": "opus"}


def collect_slugs(book: str, folder: str) -> list[str]:
    sub = img_dir(book) / folder
    if not sub.is_dir():
        raise SystemExit(f"Folder not found: {sub}")
    slugs = sorted(p.stem for p in sub.glob("*.png"))
    if not slugs:
        raise SystemExit(f"No *.png files in {sub}")
    return slugs


def printed_pages(slugs: list[str]) -> tuple[set[int], bool]:
    """Printed page numbers covered by the spreads + whether front matter is hit."""
    pages: set[int] = set()
    front = False
    for slug in slugs:
        m = re.fullmatch(r"page_(\d+)", slug)
        if m:
            n = int(m.group(1))
            pages.update((n, n + 1))
        else:
            front = True  # page_n*, page_000_cover, ...
    return pages, front


def affected_titles(book: str, slugs: list[str]) -> list[str]:
    """Source-language section titles whose text lies on the given spreads."""
    from assemble_book import STORY_STARTS  # book-specific section boundaries

    pages, front = printed_pages(slugs)
    titles: list[str] = []
    if front or (pages and min(pages) < STORY_STARTS[0][0]):
        titles.append("Przedmowa")
    for i, (start, title) in enumerate(STORY_STARTS):
        end = STORY_STARTS[i + 1][0] - 1 if i + 1 < len(STORY_STARTS) else 9999
        if any(start <= p <= end for p in pages):
            titles.append(title)
    return titles


def working_translation_files(book: str, to_lang: str) -> list[tuple[Path, str, bool]]:
    """(path, model_alias, batch) for every working translation file found."""
    found: list[tuple[Path, str, bool]] = []
    for p in sorted((book_dir(book) / "OUTPUT").glob(f"Book_{to_lang.upper()}*.md")):
        m = re.fullmatch(rf"Book_{to_lang.upper()}(_b)?_(sonn|opus)", p.stem)
        if not m:
            continue  # final suffix-less files are regenerated on completion
        found.append((p, MODEL_TAG_TO_ALIAS[m.group(2)], bool(m.group(1))))
    return found


def run_step(cmd: list[str], title: str) -> None:
    print(f"\n=== {title} ===", flush=True)
    print("RUN:", " ".join(cmd), flush=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(cmd, cwd=WORKSPACE_ROOT, env=env)
    if result.returncode != 0:
        raise SystemExit(f"Step '{title}' failed with exit code {result.returncode}")


def main() -> int:
    p = argparse.ArgumentParser(description="Re-extraction pipeline for defective page scans")
    p.add_argument("--book", default=DEFAULT_BOOK)
    p.add_argument(
        "--folder",
        required=True,
        help="Subfolder of pages_extracted/ holding the defective captures",
    )
    p.add_argument("--scan", action="store_true", help="Re-capture the spreads")
    p.add_argument("--ocr", action="store_true", help="Re-OCR the spreads")
    p.add_argument("--assemble", action="store_true", help="Rebuild Book_<lang>.* files")
    p.add_argument("--translate", action="store_true", help="Redo affected translated sections")
    p.add_argument("--to", default="RU", help="Target language for --translate (default RU)")
    args = p.parse_args()

    if not (args.scan or args.ocr or args.assemble or args.translate):
        p.error("no steps selected; use any of --scan --ocr --assemble --translate")

    py = sys.executable
    slugs = collect_slugs(args.book, args.folder)
    print(f"Spreads to re-extract ({len(slugs)}): {' '.join(slugs)}", flush=True)

    if args.scan:
        run_step(
            [py, "-u", str(SRC_OCR / "scan_pages.py"), "--book", args.book, "--slugs", *slugs],
            "scan: re-capture spreads",
        )

    if args.ocr:
        run_step(
            [
                py, "-u", str(SRC_OCR / "ocr_claude.py"),
                "--book", args.book, "--split", "--slugs", *slugs,
            ],
            "ocr: re-extract text",
        )

    if args.assemble:
        run_step(
            [py, "-u", str(SRC_OCR / "assemble_book.py"), "--book", args.book],
            "assemble: rebuild book files",
        )

    if args.translate:
        titles = affected_titles(args.book, slugs)
        if not titles:
            print("\nNo sections affected by these spreads; nothing to re-translate.", flush=True)
            return 0
        print(f"\nAffected sections: {titles}", flush=True)
        targets = working_translation_files(args.book, args.to)
        if not targets:
            print(f"No working translation files Book_{args.to.upper()}*.md found; skipping.", flush=True)
            return 0
        for path, model_alias, batch in targets:
            run_step(
                [
                    py, "-u", str(SRC_TRANSLATE / "translate_book.py"),
                    "--book", args.book, "--to", args.to,
                    "--model", model_alias,
                    "--batch" if batch else "--no-batch",
                    "--yes",
                    "--redo-titles", *titles,
                ],
                f"translate: redo affected sections in {path.name}",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
