"""
Compare re-OCR results in pages_text_recheck/ with pages_text/ and optionally
adopt the better version.

For every *.md in the recheck folder, plain text (without comments/headings)
is compared with the current file. A new version is considered better when it
contains noticeably more text (default: +5%). With --adopt, better versions
replace the originals (the old file is kept as *.md.bak next to the new one
in the recheck folder).

Usage (workspace root):
  python src\\book_ocr\\compare_recheck.py
  python src\\book_ocr\\compare_recheck.py --adopt
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from book_paths import DEFAULT_BOOK, book_dir, text_dir

RECHECK_DIRNAME = "pages_text_recheck"


def plain_text(md: str) -> str:
    md = re.sub(r"(?m)^<!--.*?-->\s*", "", md)
    md = re.sub(r"(?m)^## .+$", "", md)
    return " ".join(md.split())


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare/adopt re-OCR results")
    ap.add_argument("--book", default=DEFAULT_BOOK)
    ap.add_argument("--adopt", action="store_true", help="Replace originals with better versions")
    ap.add_argument("--min-gain", type=float, default=0.05)
    args = ap.parse_args()

    old_dir = text_dir(args.book)
    new_dir = book_dir(args.book) / RECHECK_DIRNAME
    adopted = 0
    for new_path in sorted(new_dir.glob("*.md")):
        old_path = old_dir / new_path.name
        new_len = len(plain_text(new_path.read_text(encoding="utf-8")))
        old_len = len(plain_text(old_path.read_text(encoding="utf-8"))) if old_path.exists() else 0
        gain = (new_len - old_len) / old_len if old_len else float("inf")
        better = gain >= args.min_gain
        verdict = "BETTER" if better else ("~same" if gain > -args.min_gain else "worse")
        print(f"{new_path.name:22s} old={old_len:5d} new={new_len:5d} gain={gain:+.0%}  {verdict}")
        if better and args.adopt:
            backup = new_dir / (new_path.name + ".bak")
            backup.write_text(old_path.read_text(encoding="utf-8"), encoding="utf-8")
            old_path.write_text(new_path.read_text(encoding="utf-8"), encoding="utf-8")
            adopted += 1
    if args.adopt:
        print(f"\nadopted {adopted} files (old versions saved as *.md.bak in {new_dir.name}/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
