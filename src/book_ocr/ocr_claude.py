"""
OCR book page screenshots with Claude Sonnet vision -> Markdown files.

Usage (from the workspace root):
  python src\\book_ocr\\ocr_claude.py --limit 10 --overwrite
  python src\\book_ocr\\ocr_claude.py --next-batch 10
  python src\\book_ocr\\ocr_claude.py --start-from page_016 --limit 10

Reads ANTHROPIC_API_KEY from .env in the workspace root.
Data folders default to _books/Wojna_Futbolowa/ (see --book).
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
from pathlib import Path

from anthropic import Anthropic

from book_paths import DEFAULT_BOOK, WORKSPACE_ROOT, img_dir, pages_file, text_dir

MODEL = "claude-sonnet-5"
DEFAULT_LINE_WIDTH = 60  # used if model does not report avg_full_line_chars
LINE_WIDTH_PAD = 3

PROMPT = """You are extracting text from a scan/screenshot of a Polish book (Ryszard Kapuściński, "Wojna futbolowa").

Rules:
- Transcribe ALL readable book text faithfully in Polish. Keep diacritics (ąęćłńóśźż).
- Preserve paragraph breaks (blank line between paragraphs).
- If two pages are visible (2-up), output them in reading order (left page first, then right).
- Separate the two pages with a Markdown heading like: ## Page <printed number or leaf>
- Ignore UI chrome (Archive.org controls, "Page 12 (15/247)", buttons, icons, yellow banners).
- Do not translate. Do not summarize. Do not add commentary.
- If a page is blank/cover-only/illegible, say so briefly under that heading.
- WITHIN a page: remove end-of-line hyphenation. Join split words
  (e.g. "plac-" + "ki" -> "placki", "mie-" + "szkanie" -> "mieszkanie").
  Do NOT keep soft hyphens or line-break hyphens inside a page.
- ACROSS pages: if a word is split at the page boundary (e.g. page ends with "wsta-"
  and next page starts with "wał"), keep that split at the page boundary exactly as printed.
- Inside each paragraph, write the text as ONE flowing paragraph (spaces between words).
  Do not wrap to the book's visual line breaks. A local post-processor will wrap/justify.
- Before the first ## Page heading, output exactly one HTML comment estimating the average
  character count of about 10 FULL text lines from the book pages in the image (spaces included,
  ignore page numbers and blank/short last lines of paragraphs), for example:
  <!-- avg_full_line_chars: 57 -->
- Output Markdown only (plus that one HTML comment).
"""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def slug_from_url(url: str) -> str:
    m = re.search(r"/page/([^/]+)/", url)
    if m:
        leaf = m.group(1)
        if leaf.isdigit():
            return f"page_{int(leaf):03d}"
        return f"page_{leaf}"
    return "page_000_cover"


def load_slugs(path: Path) -> list[str]:
    slugs: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        slugs.append(slug_from_url(line))
    return slugs


def extract_avg_line_chars(text: str) -> tuple[int | None, str]:
    m = re.search(r"<!--\s*avg_full_line_chars:\s*(\d+)\s*-->", text, re.I)
    if not m:
        return None, text
    width = int(m.group(1))
    cleaned = (text[: m.start()] + text[m.end() :]).lstrip("\n")
    return width, cleaned


def clean_paragraph(text: str) -> str:
    text = text.replace("\u00ad", "")  # soft hyphen
    text = re.sub(r"[ \t]+", " ", text).strip()
    # Join leftover intra-word hyphenation artifacts: "plac- ki" / "plac-ki"
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "", text)
    return text


def justify_line(words: list[str], width: int) -> str:
    if len(words) == 1:
        return words[0]
    base_len = sum(len(w) for w in words) + (len(words) - 1)
    need = width - base_len
    if need <= 0:
        return " ".join(words)
    gaps = len(words) - 1
    extras = [0] * gaps
    i = 0
    while need > 0:
        extras[i % gaps] += 1
        need -= 1
        i += 1
    parts: list[str] = []
    for idx, word in enumerate(words):
        parts.append(word)
        if idx < gaps:
            parts.append(" " * (1 + extras[idx]))
    return "".join(parts)


def wrap_and_justify_paragraph(text: str, width: int) -> str:
    words = text.split()
    if not words:
        return ""
    lines: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for word in words:
        add = len(word) if not cur else len(word) + 1
        if cur and cur_len + add > width:
            lines.append(justify_line(cur, width))
            cur = [word]
            cur_len = len(word)
        else:
            cur.append(word)
            cur_len += add
    if cur:
        lines.append(" ".join(cur))  # last line of paragraph: no justify
    return "\n".join(lines)


def format_ocr_markdown(raw: str, line_width: int | None = None) -> str:
    reported, body = extract_avg_line_chars(raw)
    width = (reported if reported and reported >= 20 else DEFAULT_LINE_WIDTH) + LINE_WIDTH_PAD
    if line_width is not None:
        width = line_width

    blocks = re.split(r"(?m)^(## .+)$", body)
    out: list[str] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if re.match(r"^## ", block or ""):
            if out and out[-1] != "":
                out.append("")
            out.append(block.strip())
            out.append("")
            i += 1
            if i >= len(blocks):
                break
            content = blocks[i]
            paragraphs = re.split(r"\n\s*\n", content.strip("\n"))
            formatted_pars: list[str] = []
            for par in paragraphs:
                lines = [ln.strip() for ln in par.splitlines() if ln.strip()]
                # drop lone page-number lines like "12"
                lines = [ln for ln in lines if not re.fullmatch(r"\d{1,4}", ln)]
                if not lines:
                    continue
                cleaned = clean_paragraph(" ".join(lines))
                if not cleaned:
                    continue
                # short notes / dates / cover labels: keep as-is
                if len(cleaned) < width * 0.6 and "\n" not in cleaned:
                    formatted_pars.append(cleaned)
                else:
                    formatted_pars.append(wrap_and_justify_paragraph(cleaned, width))
            if formatted_pars:
                out.append(formatted_pars[0])
                for par_text in formatted_pars[1:]:
                    out.append("")
                    out.append(par_text)
                out.append("")
            i += 1
            continue

        # preamble before first heading
        preamble = block.strip()
        if preamble:
            out.append(preamble)
            out.append("")
        i += 1

    text = "\n".join(out).rstrip() + "\n"
    meta = f"<!-- avg_full_line_chars: {width - LINE_WIDTH_PAD}; wrap_width: {width} -->\n"
    return meta + "\n" + text


def ocr_image(client: Anthropic, image_path: Path, model: str) -> tuple[str, dict]:
    data = base64.standard_b64encode(image_path.read_bytes()).decode("ascii")
    message = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": data,
                        },
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    )
    text_parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
    usage = {
        "input_tokens": getattr(message.usage, "input_tokens", None),
        "output_tokens": getattr(message.usage, "output_tokens", None),
    }
    return "\n".join(text_parts).strip() + "\n", usage


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OCR pages with Claude Sonnet vision")
    p.add_argument("--book", default=DEFAULT_BOOK, help="Book folder name under _books/")
    p.add_argument("--pages-file", type=Path, default=None)
    p.add_argument("--img-dir", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--start-from", default=None)
    p.add_argument(
        "--next-batch",
        type=int,
        default=None,
        help="Process the next N spreads that do not yet have .md files",
    )
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--delay", type=float, default=0.4)
    p.add_argument("--model", default=MODEL)
    args = p.parse_args()
    if args.pages_file is None:
        args.pages_file = pages_file(args.book)
    if args.img_dir is None:
        args.img_dir = img_dir(args.book)
    if args.out_dir is None:
        args.out_dir = text_dir(args.book)
    return args


def main() -> int:
    load_dotenv(WORKSPACE_ROOT / ".env")
    args = parse_args()
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key:
        print(
            "Missing ANTHROPIC_API_KEY.\n"
            f"Add to {WORKSPACE_ROOT / '.env'}:\n"
            "  ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        return 2

    if not args.pages_file.exists():
        print(f"Pages file not found: {args.pages_file}", file=sys.stderr)
        return 2

    slugs = load_slugs(args.pages_file)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    client = Anthropic(api_key=api_key)

    if args.next_batch is not None:
        pending = [s for s in slugs if not (args.out_dir / f"{s}.md").exists()]
        if not pending:
            print("No pending spreads (all .md files already exist).", flush=True)
            return 0
        slugs = pending[: args.next_batch]
        print(f"Next batch: {len(slugs)} spreads starting at {slugs[0]}", flush=True)
        work = list(enumerate(slugs, start=1))
        total_label = len(slugs)
    else:
        started = args.start_from is None
        work = []
        for idx, slug in enumerate(slugs, start=1):
            if not started:
                if slug == args.start_from:
                    started = True
                else:
                    continue
            work.append((idx, slug))
        total_label = len(load_slugs(args.pages_file))

    done = 0
    in_tok = 0
    out_tok = 0

    for idx, slug in work:
        img = args.img_dir / f"{slug}.png"
        dest = args.out_dir / f"{slug}.md"
        if not img.exists():
            print(f"[{idx}/{total_label}] MISSING image {img.name}", flush=True)
            continue
        if dest.exists() and not args.overwrite and args.next_batch is None:
            print(f"[{idx}/{total_label}] skip existing {dest.name}", flush=True)
            done += 1
            if args.limit is not None and done >= args.limit:
                break
            continue

        print(f"[{idx}/{total_label}] OCR {slug} ...", flush=True)
        text, usage = ocr_image(client, img, args.model)
        formatted = format_ocr_markdown(text)
        header = f"<!-- source: {img.name} model: {args.model} -->\n"
        dest.write_text(header + formatted, encoding="utf-8")
        if usage.get("input_tokens"):
            in_tok += usage["input_tokens"]
        if usage.get("output_tokens"):
            out_tok += usage["output_tokens"]
        print(
            f"  saved {dest.name} ({len(formatted)} chars; "
            f"in={usage.get('input_tokens')} out={usage.get('output_tokens')})",
            flush=True,
        )
        done += 1
        time.sleep(args.delay)
        if args.next_batch is None and args.limit is not None and done >= args.limit:
            break

    est = (in_tok / 1_000_000) * 2.0 + (out_tok / 1_000_000) * 10.0
    print(
        f"Finished. files={done}, input_tokens={in_tok}, output_tokens={out_tok}, "
        f"est_usd~=${est:.3f}, out={args.out_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
