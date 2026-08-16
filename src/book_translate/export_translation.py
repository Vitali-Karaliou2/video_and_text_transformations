"""
Export a finished working translation (Book_XX_b_opus.md etc.) to companion
files without mode/model suffixes: Book_XX.md, Book_XX.docx, Book_XX.pdf.

The working file keeps progress markers; companions match Book_PL structure
(H1, H2 sections, Spis treści with page-number links in docx/pdf).

Usage (workspace root):
  python src\\book_translate\\export_translation.py --book Wojna_Futbolowa --from Book_RU_b_opus.md
  python src\\book_translate\\export_translation.py --book Wojna_Futbolowa --lang RU
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from paths import DEFAULT_BOOK, WORKSPACE_ROOT, output_dir

# Reuse Word export from the Polish assemble pipeline.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "book_ocr"))
from assemble_book import (  # noqa: E402
    Section as DocSection,
    write_docx,
    write_pdf,
)
from translate_book import (  # noqa: E402
    TOC_TITLE,
    format_paragraph_for_md,
    heading_from_body,
    md_anchor,
    split_marked_blocks,
    structural_heading,
)


@dataclass
class AccSection:
    src_title: str
    heading: str | None = None
    paragraphs: list[str] = field(default_factory=list)


def discover_working(out: Path, lang: str | None, explicit: Path | None) -> Path:
    if explicit:
        path = explicit if explicit.is_absolute() else out / explicit
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    if not lang:
        raise SystemExit("Provide --from or --lang")
    # Prefer batch+opus, then batch+sonn, then realtime variants.
    candidates = [
        out / f"Book_{lang.upper()}_b_opus.md",
        out / f"Book_{lang.upper()}_b_sonn.md",
        out / f"Book_{lang.upper()}_opus.md",
        out / f"Book_{lang.upper()}_sonn.md",
        out / f"Book_{lang.upper()}.md",
    ]
    for c in candidates:
        if c.exists() and "complete" in c.read_text(encoding="utf-8")[:200]:
            return c
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"No working translation for {lang} in {out} "
        f"(looked for Book_{lang}_b_opus.md etc.)"
    )


def target_lang_from_name(path: Path) -> str:
    m = re.search(r"Book_([A-Za-z]{2})", path.name)
    if not m:
        raise ValueError(f"Cannot read language from {path.name}")
    return m.group(1).upper()


def unwrap_paragraphs(body: str) -> list[str]:
    """Collapse soft-wrapped MD paragraphs into single flowing lines for Word."""
    body = re.sub(r"(?m)^## .+\n*", "", body, count=1)
    paras: list[str] = []
    for block in re.split(r"\n\s*\n", body.strip("\n")):
        block = block.strip()
        if not block or block.startswith("- ["):
            continue
        line = re.sub(r"[ \t]+", " ", block.replace("\n", " ")).strip()
        if line:
            paras.append(line)
    return paras


def h1_from_block(body: str) -> str:
    m = re.search(r"(?m)^# (.+)$", body)
    if m:
        return m.group(1).strip()
    return re.sub(r"[ \t]+", " ", body.replace("\n", " ")).strip()


def parse_working_translation(
    path: Path, tgt_lang: str
) -> tuple[str, list[DocSection], str]:
    """Return (h1, sections for docx, toc_display_title)."""
    header, blocks = split_marked_blocks(path.read_text(encoding="utf-8"))
    if not blocks:
        raise SystemExit(f"{path.name} has no <!-- src-title / src-h1 --> markers")

    h1 = ""
    ordered: OrderedDict[str, AccSection] = OrderedDict()
    toc_display = structural_heading(tgt_lang, TOC_TITLE)

    for b in blocks:
        if b.title is None:
            h1 = h1_from_block(b.body)
            continue
        if b.title == TOC_TITLE:
            h = heading_from_body(b.body)
            if h:
                toc_display = h
            # Placeholder; filled after all display titles are known.
            ordered.setdefault(TOC_TITLE, AccSection(TOC_TITLE, toc_display, []))
            continue
        acc = ordered.setdefault(b.title, AccSection(b.title))
        h = heading_from_body(b.body)
        if h and not acc.heading:
            acc.heading = h
        acc.paragraphs.extend(unwrap_paragraphs(b.body))

    if not h1:
        raise SystemExit(f"{path.name}: missing H1 (<!-- src-h1 -->)")

    # Resolve display titles (structural fallback for front matter).
    for acc in ordered.values():
        if acc.src_title == TOC_TITLE:
            acc.heading = toc_display
            continue
        if not acc.heading:
            acc.heading = structural_heading(tgt_lang, acc.src_title)

    sections: list[DocSection] = []
    for acc in ordered.values():
        assert acc.heading
        if acc.src_title == TOC_TITLE:
            sections.append(DocSection(acc.heading, []))
        else:
            sections.append(DocSection(acc.heading, acc.paragraphs))

    toc_titles = [s.title for s in sections if s.title != toc_display]
    for s in sections:
        if s.title == toc_display:
            s.paragraphs = toc_titles
            break
    else:
        # Insert ToC after first non-H1 content section if markers lacked it.
        insert_at = 1 if sections else 0
        sections.insert(insert_at, DocSection(toc_display, toc_titles))

    return h1, sections, toc_display


def write_clean_markdown(
    path: Path, h1: str, sections: list[DocSection], toc_title: str, width: int
) -> None:
    lines: list[str] = [
        f"<!-- wrap_width: {width}; exported from working translation -->",
        "",
        f"# {h1}",
        "",
    ]
    for sec in sections:
        lines.append(f"## {sec.title}")
        lines.append("")
        if sec.title == toc_title:
            for title in sec.paragraphs:
                lines.append(f"- [{title}](#{md_anchor(title)})")
            lines.append("")
            continue
        for par in sec.paragraphs:
            formatted = format_paragraph_for_md(par, width)
            if formatted:
                lines.append(formatted)
                lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def read_wrap_width(path: Path, default: int = 63) -> int:
    head = path.read_text(encoding="utf-8")[:300]
    m = re.search(r"wrap_width:\s*(\d+)", head)
    return int(m.group(1)) if m else default


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export finished translation to md/docx/pdf")
    p.add_argument("--book", default=DEFAULT_BOOK)
    p.add_argument("--lang", default=None, help="Target language code, e.g. RU")
    p.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=None,
        help="Working translation file (default: Book_<lang>_b_opus.md)",
    )
    p.add_argument(
        "--template",
        type=Path,
        default=WORKSPACE_ROOT / "_books" / "sample_for_book_in_one_file.docx",
    )
    p.add_argument("--no-pdf", action="store_true")
    p.add_argument("--no-md", action="store_true", help="Skip writing companion Book_XX.md")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    out = output_dir(args.book)
    working = discover_working(out, args.lang, args.source)
    tgt = args.lang.upper() if args.lang else target_lang_from_name(working)
    wrap_width = read_wrap_width(working)

    print(f"Working: {working.name}", flush=True)
    print(f"Export:  Book_{tgt}.md / .docx / .pdf", flush=True)

    h1, sections, toc_title = parse_working_translation(working, tgt)
    print(f"Sections: {len(sections)} (ToC title: {toc_title})", flush=True)

    stem = f"Book_{tgt}"
    md_path = out / f"{stem}.md"
    docx_path = out / f"{stem}.docx"
    pdf_path = out / f"{stem}.pdf"

    if not args.no_md:
        write_clean_markdown(md_path, h1, sections, toc_title, wrap_width)
        print(f"Wrote {md_path}", flush=True)

    docx_written = write_docx(
        docx_path, args.template, h1, sections, toc_title=toc_title
    )
    print(f"Wrote {docx_written}", flush=True)

    if not args.no_pdf:
        try:
            written_pdf = write_pdf(docx_written, pdf_path)
            print(f"Wrote {written_pdf}", flush=True)
        except Exception as exc:
            print(f"ERROR: PDF export failed: {exc}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
