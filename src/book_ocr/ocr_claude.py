"""
OCR book page images with Claude vision -> Markdown files, one per spread.

The spreads come from whichever source the book has. A book that arrived as a
single PDF in input/ is rendered on demand by pdf_to_pages.py; one captured off
Archive.org leaf by leaf is read from pages_extracted/ in the order of its page
URL list. input/ wins when both are present.

Usage (from the workspace root):
  python src\\book_ocr\\ocr_claude.py --limit 10 --overwrite
  python src\\book_ocr\\ocr_claude.py --next-batch 10
  python src\\book_ocr\\ocr_claude.py --start-from page_016 --limit 10
  python src\\book_ocr\\ocr_claude.py --book _tutorial_spanish --profile textbook
      --next-percent 5 --split

Reads ANTHROPIC_API_KEY from .env in the workspace root.
Data folders default to _books/Wojna_Futbolowa/ (see --book).
"""

from __future__ import annotations

import argparse
import base64
import math
import os
import re
import sys
import time
from pathlib import Path

try:
    # Verify TLS against the Windows certificate store. Avast Web Shield
    # re-signs every HTTPS connection with its own root, which lives in the
    # Windows store but not in certifi's bundle, so without this every API
    # call can die with CERTIFICATE_VERIFY_FAILED.
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from anthropic import Anthropic, APIError

from book_paths import (
    DEFAULT_BOOK,
    WORKSPACE_ROOT,
    img_dir,
    pages_file,
    pdf_source,
    text_dir,
)
from pdf_to_pages import (
    DEFAULT_PAGE_LONG_SIDE,
    SPREAD_ASPECT,
    ensure_spread,
    index_for_slug,
    open_pdf,
    spread_slugs,
)

MODEL = "claude-sonnet-5"
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-5",
    "sonn": "claude-sonnet-5",
    "opus": "claude-opus-5",
}
# $/MTok input, output.
PRICE_IN_OUT = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}
DEFAULT_LINE_WIDTH = 60  # used if model does not report avg_full_line_chars
LINE_WIDTH_PAD = 3
# Textbook pages: wrap flowing prose and long word lists; structural lines
# (headings, list items, short dialogue turns) stay as the model returned them.
DEFAULT_TEXTBOOK_WRAP = 80

PROSE_PROMPT = """You are extracting text from a scan/screenshot of a Polish book (Ryszard Kapuściński, "Wojna futbolowa").

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

TEXTBOOK_PROMPT = """You are extracting text from a scan of a Russian-language textbook of Spanish
(Chernysheva et al., "Учебник испанского языка"). The scan is a photograph of a
printed book, so text lines curve slightly, especially near the gutter; read them
as the printed lines they are.

The page mixes Russian and Spanish and is laid out as teaching material:
numbered exercises with instructions in italics, word and phrase lists often set
in two columns, dialogues, phonetic transcriptions in square brackets, grammar
tables, brace diagrams, footnotes and page numbers.

Rules:
- Transcribe ALL readable text faithfully, in the language it is printed in.
  Never translate, summarise or comment.
- Keep every diacritic and special sign exactly: Spanish á é í ó ú ü ñ ¿ ¡, and
  transcription characters such as [θ], [k], [ð], [ʝ], [ŋ], stress marks (a'mor).
- If two pages are visible, output them in reading order, each under a heading
  "## Page <printed page number>". Use the number printed on that page.
- Keep the printed line structure WHERE IT CARRIES MEANING: one list item, one
  dialogue turn, one vocabulary pair, one table row per line. Two-column word
  lists read column by column only if the columns are separate lists; if a pair
  faces its translation, keep the pair on one line.
- Run ordinary prose (explanations, reading passages) into flowing paragraphs
  with a blank line between paragraphs, not one line per printed line.
- Exercise numbers stay as printed, followed by their instruction in *italics*:
  "**19.** *Перепишите вопросы...*"
- Render a brace diagram as a nested list under the letter or word it explains,
  and mark it with a line "*[схема]*" before the list, so it can be found later.
- Keep footnotes at the bottom of their page under a line "---", each starting
  with the marker printed in the book.
- Keep the printed page number out of the text; it is already in the heading.
- WITHIN a page: remove end-of-line hyphenation, joining the split word
  ("грам-" + "матический" -> "грамматический").
- ACROSS pages: if a word is split at the page boundary, keep that split exactly
  as printed.
- If a page is blank, a cover or illegible, say so in one short line under its
  heading.
- Output Markdown only, with no fenced code blocks around it.
"""

PROFILES = {"prose": PROSE_PROMPT, "textbook": TEXTBOOK_PROMPT}


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


def load_settings(path: Path) -> dict[str, str]:
    """KEY = value lines of a bat's sibling settings file; the first value wins.

    Repeating a key parks the earlier value under the current one instead of
    deleting it, as in the channel bats' settings files.
    """
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().upper()
        if key and key not in values:
            values[key] = val.strip()
    return values


def settings_defaults(values: dict[str, str]) -> dict[str, object]:
    """Translate settings-file entries into argparse defaults."""
    out: dict[str, object] = {}
    if values.get("BOOK"):
        out["book"] = values["BOOK"]
    if values.get("PROFILE"):
        out["profile"] = values["PROFILE"].lower()
    if values.get("PERCENT"):
        out["next_percent"] = float(values["PERCENT"].replace(",", "."))
    if values.get("MODEL"):
        alias = values["MODEL"].strip().lower()
        out["model"] = MODEL_ALIASES.get(alias, values["MODEL"].strip())
    if values.get("SPLIT"):
        out["split"] = values["SPLIT"].strip().lower() in {"1", "yes", "true", "on"}
    if values.get("PAGE_LONG_SIDE"):
        out["page_long_side"] = int(values["PAGE_LONG_SIDE"])
    if values.get("WRAP_WIDTH"):
        out["wrap_width"] = int(values["WRAP_WIDTH"])
    return out


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


def _is_structural_line(line: str) -> bool:
    """Headings, list items, rules and diagram markers must keep their own line."""
    s = line.strip()
    if not s:
        return True
    if re.match(r"^#{1,3}\s", s):
        return True
    if s in {"---", "***"} or s.startswith("*[") and s.endswith("]*"):
        return True
    if re.match(r"^[-*+]\s+", s) or re.match(r"^\d+\.\s+", s):
        return True
    return False


def _format_textbook_paragraph(par: str, width: int) -> str:
    """Wrap one blank-line-separated block; keep structural lines intact."""
    lines = [ln.rstrip() for ln in par.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return ""

    if all(_is_structural_line(ln) for ln in lines):
        # Nested list / scheme: wrap only the overlong items, never merge them.
        wrapped: list[str] = []
        for ln in lines:
            body = ln.strip()
            if len(body) <= width:
                wrapped.append(ln.rstrip())
                continue
            indent = re.match(r"^(\s*)", ln).group(1)
            # Keep the list marker on the first wrapped line.
            m = re.match(r"^(\s*(?:[-*+]|\d+\.)\s+)(.*)$", ln)
            if m:
                prefix, rest = m.group(1), m.group(2)
                chunk = wrap_and_justify_paragraph(rest, max(20, width - len(prefix)))
                parts = chunk.splitlines()
                wrapped.append(prefix + parts[0])
                pad = " " * len(prefix)
                for cont in parts[1:]:
                    wrapped.append(pad + cont)
            else:
                for cont in wrap_and_justify_paragraph(body, width).splitlines():
                    wrapped.append(indent + cont)
        return "\n".join(wrapped)

    # Dialogue-ish blocks: one turn per line, wrap each turn alone.
    if len(lines) > 1 and all(
        re.match(r"^[-–—]\s", ln.strip()) or _is_structural_line(ln) for ln in lines
    ):
        return "\n".join(
            ln
            if len(ln) <= width
            else wrap_and_justify_paragraph(clean_paragraph(ln), width)
            for ln in lines
        )

    cleaned = clean_paragraph(" ".join(ln.strip() for ln in lines))
    if not cleaned:
        return ""
    if len(cleaned) < width * 0.6:
        return cleaned
    return wrap_and_justify_paragraph(cleaned, width)


def format_textbook_markdown(raw: str, line_width: int | None = None) -> str:
    """Tidy a textbook transcription and wrap flowing text to ~line_width.

    Numbered exercises, word lists, dialogues and diagrams keep their block
    structure; only long prose and overlong list items are wrapped and
    right-justified, the same way as the Polish prose pages.
    """
    width = DEFAULT_TEXTBOOK_WRAP if line_width is None else line_width
    _reported, body = extract_avg_line_chars(raw)
    body = re.sub(r"(?m)^```[a-zA-Z]*\s*$", "", body)

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
            formatted_pars: list[str] = []
            for par in re.split(r"\n\s*\n", content.strip("\n")):
                formatted = _format_textbook_paragraph(par, width)
                if formatted:
                    formatted_pars.append(formatted)
            if formatted_pars:
                out.append(formatted_pars[0])
                for par_text in formatted_pars[1:]:
                    out.append("")
                    out.append(par_text)
                out.append("")
            i += 1
            continue

        preamble = block.strip()
        if preamble:
            # Rare text before the first ## Page — wrap it the same way.
            for par in re.split(r"\n\s*\n", preamble):
                formatted = _format_textbook_paragraph(par, width)
                if formatted:
                    out.append(formatted)
                    out.append("")
        i += 1

    text = "\n".join(out).rstrip() + "\n"
    meta = f"<!-- wrap_width: {width} -->\n"
    return meta + "\n" + text


def ocr_png_bytes(
    client: Anthropic, png: bytes, model: str, prompt: str
) -> tuple[str, dict]:
    data = base64.standard_b64encode(png).decode("ascii")
    message = client.messages.create(
        model=model,
        max_tokens=8192,
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
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    text_parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
    usage = {
        "input_tokens": getattr(message.usage, "input_tokens", None),
        "output_tokens": getattr(message.usage, "output_tokens", None),
    }
    if getattr(message, "stop_reason", None) == "max_tokens":
        print("  WARN: output hit max_tokens, transcription may be truncated", flush=True)
    return "\n".join(text_parts).strip() + "\n", usage


def ocr_png_with_retries(
    client: Anthropic, png: bytes, model: str, prompt: str, attempts: int = 3
) -> tuple[str, dict]:
    """Retry API errors: content-filter blocks and overloads are sometimes transient."""
    for i in range(1, attempts + 1):
        try:
            return ocr_png_bytes(client, png, model, prompt)
        except APIError as exc:
            if i == attempts:
                raise
            print(f"  WARN: API error (attempt {i}/{attempts}): {exc}", flush=True)
            time.sleep(3 * i)
    raise AssertionError("unreachable")


def split_spread(image_path: Path) -> list[bytes]:
    """Left and right page of a 2-up image; a single-page image is left whole.

    A book's cover and any single-page leaf are taller than wide, and cutting
    those down the middle would halve a page rather than separate two.
    """
    import io

    from PIL import Image

    with Image.open(image_path) as im:
        w, h = im.size
        if w <= h * SPREAD_ASPECT:
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return [buf.getvalue()]
        halves: list[bytes] = []
        for box in ((0, 0, w // 2, h), (w // 2, 0, w, h)):
            buf = io.BytesIO()
            im.crop(box).save(buf, format="PNG")
            halves.append(buf.getvalue())
    return halves


def ocr_spread_by_halves(
    client: Anthropic, image_path: Path, model: str, prompt: str
) -> tuple[str, dict, list[str]]:
    """OCR each page of the spread separately.

    Used both as the fallback for content-filter blocks (the filter judges the
    whole response, so halves often pass where the full spread does not) and as
    the normal path for dense pages, where one page per request doubles the
    pixels the model gets per book page. Returns (markdown, usage, blocked).
    """
    widths: list[int] = []
    parts: list[str] = []
    blocked: list[str] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    pages = split_spread(image_path)
    labels = ("left", "right") if len(pages) == 2 else ("page",)
    for label, png in zip(labels, pages):
        try:
            text, usage = ocr_png_with_retries(client, png, model, prompt, attempts=2)
        except APIError as exc:
            print(f"  FAILED {label} page: {exc}", flush=True)
            blocked.append(label)
            parts.append(f"## Page ({label})\n\n*[OCR failed: {exc}]*")
            continue
        width, body = extract_avg_line_chars(text)
        if width:
            widths.append(width)
        parts.append(body.strip())
        for key in usage_total:
            usage_total[key] += usage.get(key) or 0
    text = "\n\n".join(parts) + "\n"
    if widths:
        avg = round(sum(widths) / len(widths))
        text = f"<!-- avg_full_line_chars: {avg} -->\n" + text
    return text, usage_total, blocked, len(pages)


class SpreadSource:
    """The book's spreads and where their images come from.

    A PDF book has no page list of its own: the PDF enumerates the spreads and
    each one is rendered into pages_extracted/ the first time it is needed. A
    book captured from Archive.org already has its PNGs there, in the order of
    its page URL list.
    """

    def __init__(
        self,
        slugs: list[str],
        image_dir: Path,
        doc=None,
        page_long_side: int = DEFAULT_PAGE_LONG_SIDE,
    ) -> None:
        self.slugs = slugs
        self.image_dir = image_dir
        self.doc = doc
        self.page_long_side = page_long_side

    @property
    def kind(self) -> str:
        return "PDF" if self.doc is not None else "scans"

    def image_for(self, slug: str) -> Path | None:
        """Path of the spread image, rendering it from the PDF if needed."""
        dest = self.image_dir / f"{slug}.png"
        if dest.exists():
            return dest
        if self.doc is None:
            return None
        index = index_for_slug(slug)
        if index is None or not 0 <= index < self.doc.page_count:
            return None
        return ensure_spread(self.doc, index, self.image_dir, self.page_long_side)


def build_source(args: argparse.Namespace) -> SpreadSource | None:
    """Resolve where the spreads come from; a book's input/ PDF wins."""
    if args.pdf is not None:
        doc = open_pdf(args.pdf)
        return SpreadSource(
            spread_slugs(doc), args.img_dir, doc=doc, page_long_side=args.page_long_side
        )
    if args.pages_file.exists():
        return SpreadSource(load_slugs(args.pages_file), args.img_dir)
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OCR book spreads with Claude vision")
    p.add_argument("--settings", type=Path, default=None, help="KEY = value defaults file")
    p.add_argument("--book", default=DEFAULT_BOOK, help="Book folder name under _books/")
    p.add_argument("--pages-file", type=Path, default=None)
    p.add_argument("--pdf", type=Path, default=None, help="Source PDF (default: the one in input/)")
    p.add_argument("--no-pdf", action="store_true", help="Ignore input/ and read pages_extracted/")
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
    p.add_argument(
        "--next-percent",
        type=float,
        default=None,
        help="Process the next percent of the book's spreads (rounded up)",
    )
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--delay", type=float, default=0.4)
    p.add_argument("--model", default=MODEL)
    p.add_argument(
        "--profile",
        default="prose",
        choices=sorted(PROFILES),
        help="prose: flowing book text, wrapped and justified afterwards. "
        "textbook: exercises, word lists and diagrams, layout kept as printed",
    )
    p.add_argument("--page-long-side", type=int, default=DEFAULT_PAGE_LONG_SIDE)
    p.add_argument(
        "--wrap-width",
        type=int,
        default=None,
        help="MD line width for wrap/justify after OCR "
        f"(textbook default {DEFAULT_TEXTBOOK_WRAP}; prose: from the model comment)",
    )
    p.add_argument(
        "--slugs",
        nargs="+",
        default=None,
        help="Process only these spread slugs (e.g. page_008 page_042)",
    )
    p.add_argument(
        "--split",
        action="store_true",
        help="OCR each page of the spread as a separate request "
        "(more reliable for dense pages, ~same cost)",
    )
    p.add_argument(
        "--estimate-only",
        action="store_true",
        help="Show what the run would do and what it would cost, then stop",
    )

    pre, _ = p.parse_known_args()
    if pre.settings is not None:
        if not pre.settings.exists():
            p.error(f"settings file not found: {pre.settings}")
        p.set_defaults(**settings_defaults(load_settings(pre.settings)))
    args = p.parse_args()

    args.model = MODEL_ALIASES.get(args.model.lower(), args.model)
    if args.pages_file is None:
        args.pages_file = pages_file(args.book)
    if args.img_dir is None:
        args.img_dir = img_dir(args.book)
    if args.out_dir is None:
        args.out_dir = text_dir(args.book)
    if args.pdf is None and not args.no_pdf:
        args.pdf = pdf_source(args.book)
    if args.no_pdf:
        args.pdf = None
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

    source = build_source(args)
    if source is None:
        print(
            f"No spread source for book {args.book!r}: expected a PDF in "
            f"_books/{args.book}/input/ or a page list at {args.pages_file}",
            file=sys.stderr,
        )
        return 2

    slugs = source.slugs
    prompt = PROFILES[args.profile]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    client = Anthropic(api_key=api_key)

    batch_size = args.next_batch
    if args.next_percent is not None:
        batch_size = max(1, math.ceil(len(slugs) * args.next_percent / 100.0))

    print(
        f"Book {args.book}: {len(slugs)} spreads from {source.kind}, "
        f"profile={args.profile}, model={args.model}, split={args.split}",
        flush=True,
    )

    if args.slugs is not None:
        wanted = set(args.slugs)
        unknown = wanted - set(slugs)
        if unknown:
            print(f"Unknown slugs (not in this book): {sorted(unknown)}", file=sys.stderr)
            return 2
        work = [(i, s) for i, s in enumerate(slugs, start=1) if s in wanted]
        total_label = len(slugs)
        args.overwrite = True
    elif batch_size is not None:
        pending = [s for s in slugs if not (args.out_dir / f"{s}.md").exists()]
        if not pending:
            print("No pending spreads (all .md files already exist).", flush=True)
            return 0
        chosen = pending[:batch_size]
        share = len(chosen) / len(slugs) * 100
        print(
            f"Next portion: {len(chosen)} of {len(pending)} pending spreads "
            f"(~{share:.1f}% of the book), {chosen[0]}..{chosen[-1]}",
            flush=True,
        )
        work = list(enumerate(chosen, start=1))
        total_label = len(chosen)
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
        total_label = len(slugs)

    if args.estimate_only:
        # A dense book page runs ~2.2k image + ~0.4k prompt tokens in and
        # ~1.8k out; a whole spread in one request costs about the same in,
        # since the image is downsized either way, and the same out.
        per_request_in, per_request_out = 2600, 1800
        requests = len(work) * (2 if args.split else 1)
        pin, pout = PRICE_IN_OUT.get(args.model, PRICE_IN_OUT[MODEL])
        usd = (requests * per_request_in / 1e6) * pin + (
            requests * per_request_out / 1e6
        ) * pout
        print(
            f"Estimate only: {len(work)} spreads, {requests} requests, "
            f"est USD~=${usd:.2f} (no API calls made).",
            flush=True,
        )
        return 0

    done = 0
    in_tok = 0
    out_tok = 0
    failed: list[str] = []

    for idx, slug in work:
        dest = args.out_dir / f"{slug}.md"
        if dest.exists() and not args.overwrite and batch_size is None:
            print(f"[{idx}/{total_label}] skip existing {dest.name}", flush=True)
            done += 1
            if args.limit is not None and done >= args.limit:
                break
            continue

        img = source.image_for(slug)
        if img is None or not img.exists():
            print(f"[{idx}/{total_label}] MISSING image for {slug}", flush=True)
            continue

        print(f"[{idx}/{total_label}] OCR {slug} ...", flush=True)
        split_note = ""
        if args.split:
            text, usage, blocked, pages = ocr_spread_by_halves(
                client, img, args.model, prompt
            )
            split_note = "ocr-by-halves"
        else:
            try:
                text, usage = ocr_png_with_retries(
                    client, img.read_bytes(), args.model, prompt
                )
                blocked, pages = [], 1
            except APIError as exc:
                # One blocked/failed spread must not kill the batch.
                if "content filtering" not in str(exc).lower():
                    print(f"  FAILED {slug}: {exc}", flush=True)
                    failed.append(slug)
                    time.sleep(args.delay)
                    continue
                print("  blocked by content filter, retrying pages separately...", flush=True)
                text, usage, blocked, pages = ocr_spread_by_halves(
                    client, img, args.model, prompt
                )
                split_note = "ocr-by-halves"

        if blocked and len(blocked) == pages:
            print(f"  FAILED {slug}: no page came back", flush=True)
            failed.append(slug)
            time.sleep(args.delay)
            continue
        if blocked:
            failed.append(f"{slug} ({blocked[0]} page only)")
            split_note = f"{split_note}, {blocked[0]} page blocked".lstrip(", ")

        if args.profile == "textbook":
            wrap = args.wrap_width if args.wrap_width is not None else DEFAULT_TEXTBOOK_WRAP
            formatted = format_textbook_markdown(text, line_width=wrap)
        else:
            formatted = format_ocr_markdown(text, line_width=args.wrap_width)
        header = f"<!-- source: {img.name} model: {args.model} profile: {args.profile} -->\n"
        if split_note:
            header += f"<!-- {split_note} -->\n"
        dest.write_text(header + formatted, encoding="utf-8")
        in_tok += usage.get("input_tokens") or 0
        out_tok += usage.get("output_tokens") or 0
        print(
            f"  saved {dest.name} ({len(formatted)} chars; "
            f"in={usage.get('input_tokens')} out={usage.get('output_tokens')})",
            flush=True,
        )
        done += 1
        time.sleep(args.delay)
        if batch_size is None and args.limit is not None and done >= args.limit:
            break

    pin, pout = PRICE_IN_OUT.get(args.model, PRICE_IN_OUT[MODEL])
    est = (in_tok / 1_000_000) * pin + (out_tok / 1_000_000) * pout
    remaining = sum(1 for s in slugs if not (args.out_dir / f"{s}.md").exists())
    print(
        f"Finished. files={done}, input_tokens={in_tok}, output_tokens={out_tok}, "
        f"est_usd~=${est:.3f}, out={args.out_dir}",
        flush=True,
    )
    print(
        f"Book progress: {len(slugs) - remaining}/{len(slugs)} spreads done, "
        f"{remaining} left.",
        flush=True,
    )
    if failed:
        print(f"FAILED spreads ({len(failed)}): {', '.join(failed)}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
