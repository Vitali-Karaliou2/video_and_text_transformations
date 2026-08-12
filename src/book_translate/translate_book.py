"""
Translate a assembled book (OUTPUT/Book_XX.*) into another language in portions.

Source language is taken from the last two letters of the Book_XX stem.
Target language is --to (e.g. RU). Each run translates the next portion of the
main content (default 10%, accepted anywhere in the 8-12% window).

How a portion is cut, coarsest first, each rule used only when the previous one
cannot land inside the window:
  1. whole sections;
  2. a paragraph boundary, nearest below the top of the window;
  3. a sentence boundary, nearest below the top of the window;
  4. neither: the run refuses and explains, since such a text cannot be
     portioned at all.
A run never takes less than MIN_PORTION_CHARS, however small the requested
percentage; if less than that is left, the rest goes in a single run.

A section may therefore be translated across several runs. Progress is kept in
the working file as a hidden marker per block naming the source section and,
for a partial one, how many of its sentences are done.

While translation is incomplete, only one working file is written, matching the
source format, with mode/model suffixes:
  Book_RU_b_sonn.md   batch + Sonnet
  Book_RU_b_opus.md   batch + Opus
  Book_RU_sonn.md     realtime + Sonnet
  Book_RU_opus.md     realtime + Opus

When the book is fully translated, companion .docx/.pdf (or .md) are generated
without mode/model suffixes.

Usage (workspace root):
  python src\\book_translate\\translate_book.py --book Wojna_Futbolowa --to RU --estimate-only
  python src\\book_translate\\translate_book.py --book Wojna_Futbolowa --to RU --model sonnet --batch
  python src\\book_translate\\translate_book.py --book Wojna_Futbolowa --to RU --model opus --batch --portion 10

Re-translating sections whose source text changed (e.g. after re-extracting
lost pages), in place, only if they are already present in the working file:
  python src\\book_translate\\translate_book.py --book Wojna_Futbolowa --to RU \\
      --model sonnet --batch --yes --redo-titles "Lumumba" "Prezesi"

Applying an already-ended Message Batch after a local wait was interrupted
(no new generation charge; portion must still match current progress):
  python src\\book_translate\\translate_book.py --book Wojna_Futbolowa --to RU \\
      --model opus --batch --yes --apply-batch msgbatch_01...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from paths import DEFAULT_BOOK, WORKSPACE_ROOT, output_dir

DEFAULT_PORTION = 10.0
PORTION_LO = 0.8
PORTION_HI = 1.2
# Splitting text finer than this is not worth the loss of context, so a run
# never asks for less, whatever percentage was requested. If less than this
# is left to translate, the rest goes in one run.
MIN_PORTION_CHARS = 5000
# One API answer must stay well below MAX_TOKENS; long sections are therefore
# sent in several requests. Russian output runs ~2.7 chars per token, so this
# leaves a wide margin.
MAX_REQUEST_CHARS = 12000
MAX_TOKENS = 16384
DEFAULT_WRAP_WIDTH = 63  # same as Book_PL.md (avg_full_line + 3)
FRONT_TITLES = {"Przedmowa", "Spis treści", "Nota końcowa"}
# Titles that are not counted in the "main content" portion meter.
NON_MAIN_TITLES = {"Spis treści"}

MODEL_ALIASES = {
    "sonnet": "claude-sonnet-5",
    "sonn": "claude-sonnet-5",
    "opus": "claude-opus-5",
}
MODEL_FILE_TAG = {
    "claude-sonnet-5": "sonn",
    "claude-opus-5": "opus",
}

# Intro pricing through 2026-08-31; standard thereafter (approximate).
PRICE_IN_OUT = {
    "claude-sonnet-5": (2.0, 10.0),  # $/MTok input, output (intro)
    "claude-opus-5": (5.0, 25.0),
}
BATCH_DISCOUNT = 0.5

LANG_NAMES = {
    "PL": "Polish",
    "RU": "Russian",
    "EN": "English",
}


@dataclass
class Section:
    title: str  # "" for preamble / H1 block before first ##
    kind: str  # h1 | h2
    paragraphs: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(len(p.split()) for p in self.paragraphs)

    @property
    def is_main_story(self) -> bool:
        return self.kind == "h2" and self.title not in FRONT_TITLES


@dataclass
class Unit:
    """One sentence — the finest grain a portion may be cut at."""

    text: str
    para: int
    para_start: bool


@dataclass
class Piece:
    """A run of units of one section: what a single run translates of it."""

    section: Section
    start: int  # index of the first unit
    end: int  # index after the last unit
    total_units: int

    @property
    def units(self) -> list[Unit]:
        return section_units(self.section)[self.start : self.end]

    @property
    def chars(self) -> int:
        return units_chars(self.units)

    @property
    def is_section_start(self) -> bool:
        return self.start == 0

    @property
    def is_section_end(self) -> bool:
        return self.end >= self.total_units


# Sentence end: terminal punctuation plus optional closing quote/bracket.
# Common Polish abbreviations must not be mistaken for one.
SENTENCE_END_RE = re.compile(r'(?<=[.!?…])["»”\')\]]*\s+')
ABBREVIATIONS = {
    "np", "itd", "itp", "tzw", "tj", "ok", "r", "w", "ul", "dr", "prof", "mgr",
    "godz", "str", "nr", "cd", "m", "in", "por", "gen", "płk", "św", "ww", "jw",
}


def split_sentences(paragraph: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for m in SENTENCE_END_RE.finditer(paragraph):
        head = paragraph[start : m.start()]
        last_word = re.split(r"[\s(«„\"]", head.rstrip("."))[-1].lower()
        # A lone initial or a known abbreviation does not end a sentence.
        if last_word in ABBREVIATIONS or len(last_word) <= 1:
            continue
        parts.append(head.strip())
        start = m.end()
    tail = paragraph[start:].strip()
    if tail:
        parts.append(tail)
    return parts or [paragraph.strip()]


_UNITS_CACHE: dict[int, list[Unit]] = {}


def section_units(sec: Section) -> list[Unit]:
    cached = _UNITS_CACHE.get(id(sec))
    if cached is not None:
        return cached
    units: list[Unit] = []
    for p_idx, par in enumerate(sec.paragraphs):
        for s_idx, sentence in enumerate(split_sentences(par)):
            units.append(Unit(sentence, p_idx, s_idx == 0))
    _UNITS_CACHE[id(sec)] = units
    return units


def units_chars(units: list[Unit]) -> int:
    return sum(len(u.text) + 1 for u in units)


def units_to_paragraphs(units: list[Unit]) -> list[str]:
    paragraphs: list[str] = []
    current: list[str] = []
    prev_para: int | None = None
    for u in units:
        if prev_para is not None and u.para != prev_para:
            paragraphs.append(" ".join(current))
            current = []
        current.append(u.text)
        prev_para = u.para
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def discover_source(out: Path, explicit: Path | None) -> Path:
    if explicit:
        if not explicit.exists():
            raise FileNotFoundError(explicit)
        return explicit
    # Prefer Markdown: structured, text-native, best for chunked translation.
    md = sorted(out.glob("Book_??.md"))
    if md:
        return md[0]
    docx = sorted(out.glob("Book_??.docx"))
    if docx:
        return docx[0]
    raise FileNotFoundError(f"No Book_XX.md/.docx in {out}")


def source_lang_from_name(path: Path) -> str:
    m = re.search(r"Book_([A-Za-z]{2})(?:_|\.|$)", path.name)
    if not m:
        raise ValueError(f"Cannot read source language from filename: {path.name}")
    return m.group(1).upper()


def working_stem(target_lang: str, batch: bool, model_id: str) -> str:
    tag = MODEL_FILE_TAG[model_id]
    parts = [f"Book_{target_lang.upper()}"]
    if batch:
        parts.append("b")
    parts.append(tag)
    return "_".join(parts)


def parse_book_md(text: str) -> tuple[str, list[Section]]:
    """Return (h1_text, sections). H1 may be empty."""
    text = re.sub(r"(?m)^<!--.*?-->\s*", "", text).lstrip()
    h1 = ""
    m = re.match(r"^# (.+?)(?:\n\n|\n(?=## ))", text, re.S)
    if m:
        h1 = m.group(1).strip()
        text = text[m.end() :]

    sections: list[Section] = []
    parts = re.split(r"(?m)^(## .+)$", text)
    # preamble before first ##
    preamble = parts[0].strip()
    if preamble:
        paras = [p.strip() for p in re.split(r"\n\s*\n", preamble) if p.strip()]
        if paras:
            sections.append(Section(title="", kind="preamble", paragraphs=paras))

    i = 1
    while i < len(parts):
        title = parts[i][3:].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        paras: list[str] = []
        for block in re.split(r"\n\s*\n", body.strip("\n")):
            block = block.strip()
            if not block or block.startswith("- ["):
                continue  # skip TOC link lists
            # collapse soft wraps / justify spaces into flowing paragraph
            line = re.sub(r"[ \t]+", " ", block.replace("\n", " ")).strip()
            if line:
                paras.append(line)
        sections.append(Section(title=title, kind="h2", paragraphs=paras))
        i += 2
    return h1, sections


### Progress tracking in the working translation file
#
# Headings in the target file are translated, and the model may reword, drop
# or re-level them, so they cannot identify what is already done. Each
# translated block is therefore preceded by a hidden marker naming its SOURCE
# section, which makes progress tracking exact.
H1_MARK = "<!-- src-h1 -->"
BLOCK_MARK_RE = re.compile(
    r"(?m)^<!--\s*src-(?:h1"
    r"|title:\s*(?P<title>.+?)(?:;\s*sentences:\s*(?P<done>\d+)/(?P<total>\d+))?)"
    r"\s*-->[ \t]*$"
)


def src_mark(title: str, done: int | None = None, total: int | None = None) -> str:
    """Marker for a translated block; a partial section records its progress."""
    if done is None or total is None or done >= total:
        return f"<!-- src-title: {title} -->"
    return f"<!-- src-title: {title}; sentences: {done}/{total} -->"


@dataclass
class MarkedBlock:
    title: str | None  # None = the book's H1
    body: str
    done: int | None = None  # sentences of the section translated so far
    total: int | None = None


def split_marked_blocks(text: str) -> tuple[str, list[MarkedBlock]]:
    """Split a working file into its header and its marked blocks."""
    marks = list(BLOCK_MARK_RE.finditer(text))
    if not marks:
        return text, []
    header = text[: marks[0].start()].strip("\n")
    blocks: list[MarkedBlock] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end() : end].strip("\n")
        done = int(m.group("done")) if m.group("done") else None
        total = int(m.group("total")) if m.group("total") else None
        blocks.append(MarkedBlock(m.group("title"), body, done, total))
    return header, blocks


def translated_progress(dest: Path) -> dict[str, int | None]:
    """Source title -> sentences already translated (None = whole section)."""
    if not dest.exists():
        return {}
    _header, blocks = split_marked_blocks(dest.read_text(encoding="utf-8"))
    progress: dict[str, int | None] = {}
    for b in blocks:
        if not b.title:
            continue
        if b.done is None:
            progress[b.title] = None  # complete
        elif progress.get(b.title, 0) is not None:
            progress[b.title] = max(progress.get(b.title) or 0, b.done)
    return progress


def units_done(progress: dict[str, int | None], sec: Section) -> int:
    if sec.title not in progress:
        return 0
    done = progress[sec.title]
    return len(section_units(sec)) if done is None else done


def ensure_markers(dest: Path) -> bool:
    """True if the file can be tracked (is new, or already carries markers)."""
    if not dest.exists() or not dest.read_text(encoding="utf-8").strip():
        return True
    return bool(BLOCK_MARK_RE.search(dest.read_text(encoding="utf-8")))


class PortionError(Exception):
    """The text cannot be cut anywhere near the requested portion size."""


def select_next_pieces(
    sections: list[Section],
    progress: dict[str, int | None],
    portion_pct: float,
) -> tuple[list[Piece], str]:
    """Choose what to translate next, cutting as coarsely as the size allows.

    The portion is measured in characters against the whole main content. Cuts
    are preferred in this order, each one finer than the last and used only
    when the coarser one cannot land inside the [lo, hi] window:
    section boundary, paragraph boundary, sentence boundary. A text that
    offers none of them within the window cannot be portioned at all.
    """
    main = [s for s in sections if s.is_main_story]
    total = sum(units_chars(section_units(s)) for s in main) or 1
    target = max(total * portion_pct / 100.0, MIN_PORTION_CHARS)
    lo, hi = target * PORTION_LO, target * PORTION_HI

    pending: list[tuple[Section, int, int]] = []  # section, first unit, unit count
    for sec in main:
        n = len(section_units(sec))
        start = units_done(progress, sec)
        if start < n:
            pending.append((sec, start, n))
    if not pending:
        return [], ""

    remaining = sum(units_chars(section_units(s)[start:]) for s, start, _ in pending)
    if remaining <= hi:
        return [Piece(s, start, n, n) for s, start, n in pending], "all that is left"

    pieces: list[Piece] = []
    cum = 0
    for sec, start, n in pending:
        units = section_units(sec)[start:]
        sec_chars = units_chars(units)
        if cum + sec_chars <= hi:
            pieces.append(Piece(sec, start, n, n))
            cum += sec_chars
            if cum >= lo:
                return pieces, "section boundary"
            continue

        # The section overshoots the window: cut inside it, as coarsely as
        # possible. Candidates are cut points whose total lands in [lo, hi].
        cut_para = cut_sentence = None
        consumed = 0
        for i in range(1, len(units)):
            consumed += len(units[i - 1].text) + 1
            if cum + consumed > hi:
                break
            if cum + consumed >= lo:
                cut_sentence = i
                if units[i].para_start:
                    cut_para = i
        cut = cut_para or cut_sentence
        if cut is None:
            raise PortionError(
                f"section '{sec.title}' offers no paragraph or sentence boundary "
                f"between {lo:.0f} and {hi:.0f} characters"
            )
        pieces.append(Piece(sec, start, start + cut, n))
        return pieces, "paragraph boundary" if cut_para else "sentence boundary"

    return pieces, "section boundary"


def estimate_tokens(chars: int) -> tuple[int, int]:
    """Rough input/output token estimate for costing."""
    # Polish ~3.5 chars/token; output RU similar + prompt overhead
    return int(chars / 3.5) + 800, int(chars / 3.2) + 200


def estimate_usd(model_id: str, inp: int, out: int, batch: bool) -> float:
    pin, pout = PRICE_IN_OUT[model_id]
    cost = (inp / 1_000_000) * pin + (out / 1_000_000) * pout
    if batch:
        cost *= BATCH_DISCOUNT
    return cost


TRANSLATE_SYSTEM = """You are a literary translator. Translate the user's Markdown book fragment \
from {src} into {tgt}.

Rules:
- Preserve Markdown structure exactly: keep ## headings; keep paragraph breaks.
- Translate heading titles into {tgt} (natural literary titles, not transliteration unless a proper name).
- Translate body text faithfully; keep the author's voice (reportage / Kapuściński style).
- Keep personal names, place names, and well-known foreign phrases as appropriate for {tgt} literary practice.
- Do not summarize, omit, or add commentary.
- Do not wrap lines or insert extra blank lines beyond paragraph separation.
- A fragment that starts without a heading continues the previous one: translate
  it as running text and do not invent a heading for it.
- Output Markdown only.
"""


def fragment_user_message(h1: str | None, sections: list[Section]) -> str:
    parts: list[str] = []
    if h1:
        parts.append(f"# {h1}")
        parts.append("")
    for s in sections:
        if s.title:
            parts.append(f"## {s.title}")
            parts.append("")
        for p in s.paragraphs:
            parts.append(p)
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def chunk_units(units: list[Unit], limit: int = MAX_REQUEST_CHARS) -> list[list[Unit]]:
    """Split units into request-sized groups, breaking at paragraph starts."""
    chunks: list[list[Unit]] = []
    current: list[Unit] = []
    size = 0
    for u in units:
        add = len(u.text) + 1
        # Break before a new paragraph; an oversized paragraph breaks mid-way.
        if current and size + add > limit and (u.para_start or size >= limit):
            chunks.append(current)
            current = []
            size = 0
        current.append(u)
        size += add
    if current:
        chunks.append(current)
    return chunks


def piece_requests(piece: Piece) -> list[str]:
    """User messages translating a piece, small enough for one answer each."""
    messages: list[str] = []
    for i, chunk in enumerate(chunk_units(piece.units)):
        title = piece.section.title if (i == 0 and piece.is_section_start) else ""
        messages.append(
            fragment_user_message(
                None, [Section(title=title, kind="h2", paragraphs=units_to_paragraphs(chunk))]
            )
        )
    return messages


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
        lines.append(" ".join(cur))  # last line: no justify
    return "\n".join(lines)


def format_paragraph_for_md(text: str, width: int) -> str:
    cleaned = re.sub(r"[ \t]+", " ", text.replace("\n", " ")).strip()
    if not cleaned:
        return ""
    if len(cleaned) < width * 0.6:
        return cleaned
    return wrap_and_justify_paragraph(cleaned, width)


def format_translated_markdown(raw: str, width: int) -> str:
    """Apply the same wrap/justify rules as Book_PL.md body paragraphs."""
    raw = raw.strip()
    if not raw:
        return ""
    lines_out: list[str] = []
    # Keep leading HTML comments as-is
    while raw.startswith("<!--"):
        end = raw.find("-->")
        if end < 0:
            break
        comment = raw[: end + 3].strip()
        lines_out.append(comment)
        lines_out.append("")
        raw = raw[end + 3 :].lstrip()

    parts = re.split(r"(?m)^(#{1,2} .+)$", raw)
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        if re.match(r"^#{1,2} ", part):
            if lines_out and lines_out[-1] != "":
                lines_out.append("")
            lines_out.append(part.strip())
            lines_out.append("")
            continue
        for par in re.split(r"\n\s*\n", part.strip("\n")):
            par = par.strip()
            if not par or par.startswith("- ["):
                if par.startswith("- ["):
                    lines_out.append(par)
                    lines_out.append("")
                continue
            formatted = format_paragraph_for_md(par, width)
            if formatted:
                lines_out.append(formatted)
                lines_out.append("")
    return "\n".join(lines_out).rstrip() + "\n"


def read_source_wrap_width(source: Path, fallback: int = DEFAULT_WRAP_WIDTH) -> int:
    try:
        head = source.read_text(encoding="utf-8")[:500]
    except OSError:
        return fallback
    m = re.search(r"wrap_width:\s*(\d+)", head)
    return int(m.group(1)) if m else fallback


def append_translated_blocks(
    dest: Path, blocks: list[tuple[str, str]], wrap_width: int
) -> None:
    """Append (marker, raw translation) pairs to the working file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    is_new = not dest.exists() or not dest.read_text(encoding="utf-8").strip()
    if is_new:
        out.append(
            f"<!-- translated by book_translate; wrap_width: {wrap_width}; "
            f"incomplete until all stories done -->"
        )
    else:
        out.append(dest.read_text(encoding="utf-8").rstrip())

    for marker, raw in blocks:
        body = format_translated_markdown(raw, wrap_width).strip("\n")
        if not body:
            continue
        if marker == H1_MARK:
            # The book title must stay an H1 even if the model returned "## ".
            body = re.sub(r"^#+\s+", "# ", body)
        out.append(marker)
        out.append(body)
    dest.write_text("\n\n".join(out) + "\n", encoding="utf-8")

def check_complete(msg, label: str) -> None:
    """A translation cut off by the token limit must never reach the file."""
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise RuntimeError(
            f"{label}: the answer hit the {MAX_TOKENS}-token limit and is cut off "
            f"mid-text. Lower MAX_REQUEST_CHARS (now {MAX_REQUEST_CHARS}) and retry."
        )


def call_realtime(client, model_id: str, system: str, user: str) -> tuple[str, dict]:
    msg = client.messages.create(
        model=model_id,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    check_complete(msg, "realtime request")
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
    }
    return text.strip() + "\n", usage


def parse_batch_slot(custom_id: str) -> tuple[int, int]:
    """Map a batch custom_id to (piece_index, chunk_index); piece -1 = H1."""
    if custom_id.endswith("-h1"):
        return (-1, 0)
    m = re.search(r"-p(\d+)-(\d+)$", custom_id)
    if not m:
        raise RuntimeError(f"Unrecognized batch custom_id: {custom_id}")
    return int(m.group(1)), int(m.group(2))


def fetch_ended_batch_results(client, batch_id: str) -> tuple[dict[str, str], dict]:
    """Load texts/usage from an already-ended Message Batch (no new spend)."""
    batch = client.messages.batches.retrieve(batch_id)
    status = batch.processing_status
    counts = getattr(batch, "request_counts", None)
    extra = ""
    if counts:
        extra = (
            f" succeeded={getattr(counts, 'succeeded', '?')}"
            f" errored={getattr(counts, 'errored', '?')}"
            f" processing={getattr(counts, 'processing', '?')}"
        )
    print(f"Batch {batch_id}: status={status}{extra}", flush=True)
    if status != "ended":
        raise RuntimeError(
            f"Batch {batch_id} is not finished yet (status={status}). "
            "Wait for it to end, or keep polling with a normal --batch run."
        )

    texts: dict[str, str] = {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            raise RuntimeError(f"Batch item {result.custom_id} failed: {result.result}")
        msg = result.result.message
        check_complete(msg, f"batch item {result.custom_id}")
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        texts[result.custom_id] = text.strip() + "\n"
        usage["input_tokens"] += getattr(msg.usage, "input_tokens", 0) or 0
        usage["output_tokens"] += getattr(msg.usage, "output_tokens", 0) or 0
    return texts, usage


def texts_for_slots(
    texts_map: dict[str, str], expected_slots: list[tuple[int, int]]
) -> list[str]:
    """Order batch answers to match the request slots of the current portion."""
    got = {parse_batch_slot(cid): text for cid, text in texts_map.items()}
    if set(got) != set(expected_slots):
        raise RuntimeError(
            "Batch results do not match the portion currently pending in the "
            f"working file.\n  expected slots: {expected_slots}\n  got slots: "
            f"{sorted(got)}\nRefuse to append: the next portion may have changed, "
            "or this batch belongs to a different run."
        )
    return [got[slot] for slot in expected_slots]


def run_batch_multi(
    client, model_id: str, system: str, items: list[tuple[str, str]]
) -> tuple[dict[str, str], dict]:
    """items: list of (custom_id, user_message). Returns texts by custom_id + total usage."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    requests = [
        Request(
            custom_id=cid,
            params=MessageCreateParamsNonStreaming(
                model=model_id,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
            ),
        )
        for cid, user in items
    ]
    batch = client.messages.batches.create(requests=requests)
    print(f"Batch submitted: {batch.id} ({len(requests)} requests, status={batch.processing_status})", flush=True)
    print("Waiting for batch (up to 24h; polling every 30s)…", flush=True)
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        status = batch.processing_status
        counts = getattr(batch, "request_counts", None)
        extra = ""
        if counts:
            extra = (
                f" succeeded={getattr(counts, 'succeeded', '?')}"
                f" errored={getattr(counts, 'errored', '?')}"
                f" processing={getattr(counts, 'processing', '?')}"
            )
        print(f"  status={status}{extra}", flush=True)
        if status == "ended":
            break
        if status in {"canceling", "canceled"}:
            raise RuntimeError(f"Batch {batch.id} canceled")
        time.sleep(30)

    texts, usage = fetch_ended_batch_results(client, batch.id)
    missing = [cid for cid, _ in items if cid not in texts]
    if missing:
        raise RuntimeError(f"Missing batch results for: {missing}")
    return texts, usage


def run_batch_and_wait(client, model_id: str, system: str, user: str, custom_id: str) -> tuple[str, dict]:
    texts, usage = run_batch_multi(client, model_id, system, [(custom_id, user)])
    return texts[custom_id], usage


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Translate Book_XX.* in portions")
    p.add_argument("--book", default=DEFAULT_BOOK)
    p.add_argument("--to", required=True, help="Target language code, e.g. RU")
    p.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Explicit source file (default: Book_??.md in OUTPUT)",
    )
    p.add_argument("--portion", type=float, default=DEFAULT_PORTION, help="Percent of main content (~8–12%% window)")
    p.add_argument(
        "--model",
        default="sonnet",
        choices=sorted(MODEL_ALIASES.keys()),
        help="Model alias (default: sonnet)",
    )
    p.add_argument("--batch", action=argparse.BooleanOptionalAction, default=True, help="Use Message Batches API (default: on)")
    p.add_argument("--estimate-only", action="store_true")
    p.add_argument("--yes", action="store_true", help="Do not ask for confirmation after cost estimate")
    p.add_argument(
        "--include-front",
        action="store_true",
        help="On first portion, also translate H1 + Przedmowa",
    )
    p.add_argument(
        "--wrap-width",
        type=int,
        default=None,
        help=f"MD line width for wrap/justify (default: from source comment or {DEFAULT_WRAP_WIDTH})",
    )
    p.add_argument(
        "--redo-titles",
        nargs="+",
        default=None,
        help="Re-translate these source-language section titles IN PLACE in the "
        "working file (sections not translated yet are skipped). Used when the "
        "source text of already-translated sections has changed.",
    )
    p.add_argument(
        "--apply-batch",
        metavar="BATCH_ID",
        default=None,
        help="Do not submit a new batch: pull an already-ended Message Batch "
        "(e.g. after the local wait was interrupted) and append it as the "
        "next pending portion. No extra API spend for generation.",
    )
    return p.parse_args()


def run_requests(
    client, model_id: str, system: str, items: list[tuple[str, str]], batch: bool
) -> tuple[dict, list[str]]:
    """Run all requests, returning total usage and answers in item order."""
    if batch:
        texts_map, usage = run_batch_multi(client, model_id, system, items)
        return usage, [texts_map[cid] for cid, _ in items]
    usage = {"input_tokens": 0, "output_tokens": 0}
    texts: list[str] = []
    for i, (_cid, user) in enumerate(items, start=1):
        print(f"  request {i}/{len(items)} ...", flush=True)
        text, u = call_realtime(client, model_id, system, user)
        texts.append(text)
        usage["input_tokens"] += u["input_tokens"]
        usage["output_tokens"] += u["output_tokens"]
    return usage, texts


def make_client_and_system(src_lang: str, tgt_lang: str):
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    if not api_key:
        print("Missing ANTHROPIC_API_KEY in .env", file=sys.stderr)
        return None, None

    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    system = TRANSLATE_SYSTEM.format(
        src=LANG_NAMES.get(src_lang, src_lang),
        tgt=LANG_NAMES.get(tgt_lang, tgt_lang),
    )
    return client, system


def redo_sections(
    args: argparse.Namespace,
    dest: Path,
    sections: list[Section],
    src_lang: str,
    tgt_lang: str,
    model_id: str,
    wrap_width: int,
) -> int:
    """Re-translate already-translated sections in place (source text changed)."""
    wanted = set(args.redo_titles)
    unknown = wanted - {s.title for s in sections}
    if unknown:
        print(f"WARN: titles not found in source, ignored: {sorted(unknown)}", flush=True)
    started = set(translated_progress(dest))
    by_title = {s.title: s for s in sections}
    todo = [by_title[t] for t in args.redo_titles if t in started]
    skipped = wanted - unknown - started
    if skipped:
        print(
            f"Not translated yet, nothing to redo (will be picked up by future "
            f"portions): {sorted(skipped)}",
            flush=True,
        )
    if not todo:
        print(f"No already-translated sections to redo in {dest.name}.", flush=True)
        return 0

    words = sum(sec.word_count for sec in todo)
    inp_est, out_est = estimate_tokens(sum(len(u.text) + 1 for s in todo for u in section_units(s)))
    usd = estimate_usd(model_id, inp_est, out_est, args.batch)
    print("=== redo translation estimate ===", flush=True)
    print(f"Target:  {dest.name}  ({src_lang} -> {tgt_lang})", flush=True)
    print(f"Model:   {model_id}  batch={args.batch}  wrap_width={wrap_width}", flush=True)
    print("Sections to re-translate in place:", flush=True)
    for sec in todo:
        print(f"  - {sec.title}  ({sec.word_count} words)", flush=True)
    print(f"Words~={words}; est tokens in~={inp_est} out~={out_est}; est USD~=${usd:.3f}", flush=True)
    if args.estimate_only:
        return 0
    if not args.yes:
        try:
            ans = input("Proceed with redo? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in {"y", "yes"}:
            print("Aborted.", flush=True)
            return 1

    client, system = make_client_and_system(src_lang, tgt_lang)
    if client is None:
        return 2

    stamp = int(time.time())
    items: list[tuple[str, str]] = []
    owner: list[str] = []  # section title each request belongs to
    for i, sec in enumerate(todo):
        units = section_units(sec)
        for j, user in enumerate(piece_requests(Piece(sec, 0, len(units), len(units)))):
            items.append((f"{stamp}-redo{i}-{j}", user))
            owner.append(sec.title)
    usage, texts = run_requests(client, model_id, system, items, args.batch)

    fresh: dict[str, str] = {}
    for title, text in zip(owner, texts):
        fresh[title] = (fresh.get(title, "") + "\n\n" + text.strip()).strip()

    # Replace the marked blocks in place, keeping file order untouched. A
    # section translated in several runs collapses back into one block.
    header, blocks = split_marked_blocks(dest.read_text(encoding="utf-8"))
    written: set[str] = set()
    out_parts = [header] if header else []
    for b in blocks:
        if b.title in fresh:
            if b.title in written:
                continue  # drop the leftover partial blocks of this section
            written.add(b.title)
            out_parts.append(src_mark(b.title))
            out_parts.append(format_translated_markdown(fresh[b.title], wrap_width).strip("\n"))
            continue
        out_parts.append(H1_MARK if b.title is None else src_mark(b.title, b.done, b.total))
        out_parts.append(b.body)
    dest.write_text("\n\n".join(p for p in out_parts if p) + "\n", encoding="utf-8")

    pin, pout = PRICE_IN_OUT[model_id]
    actual = (usage["input_tokens"] / 1e6) * pin + (usage["output_tokens"] / 1e6) * pout
    if args.batch:
        actual *= BATCH_DISCOUNT
    print(
        f"Redone {len(todo)} section(s) in {dest}  tokens in={usage['input_tokens']} "
        f"out={usage['output_tokens']} actual_usd~=${actual:.3f}",
        flush=True,
    )
    return 0


def main() -> int:
    args = parse_args()
    load_dotenv(WORKSPACE_ROOT / ".env")
    out = output_dir(args.book)
    source = discover_source(out, args.source)
    src_lang = source_lang_from_name(source)
    tgt_lang = args.to.upper()
    model_id = MODEL_ALIASES[args.model]
    wrap_width = args.wrap_width or read_source_wrap_width(source)
    if source.suffix.lower() != ".md":
        print(
            f"ERROR: progressive translation currently supports Markdown source only "
            f"(got {source.name}). Prefer Book_{src_lang}.md.",
            file=sys.stderr,
        )
        return 2

    h1, sections = parse_book_md(source.read_text(encoding="utf-8"))
    dest = out / f"{working_stem(tgt_lang, args.batch, model_id)}.md"

    if not ensure_markers(dest):
        print(
            f"ERROR: {dest.name} has no <!-- src-title: ... --> markers, so the script\n"
            "cannot tell which sections are already translated and would duplicate\n"
            "them. Add the markers (one line before each translated section, naming\n"
            f"its {src_lang} title) or start a fresh file.",
            file=sys.stderr,
        )
        return 2

    if args.redo_titles:
        return redo_sections(args, dest, sections, src_lang, tgt_lang, model_id, wrap_width)

    progress = translated_progress(dest)
    try:
        portion, cut_note = select_next_pieces(sections, progress, args.portion)
    except PortionError as exc:
        print(
            f"ERROR: cannot split the text into portions: {exc}.\n"
            "Check the source: a text without paragraph or sentence breaks of a "
            "usable size cannot be translated in portions. Raise --portion to take "
            "the whole section at once.",
            file=sys.stderr,
        )
        return 2
    if not portion:
        print("Nothing left to translate (all main stories present in target file).", flush=True)
        return 0

    include_front = args.include_front or (not dest.exists())
    front: list[Piece] = []
    if include_front:
        for s in sections:
            if s.title == "Przedmowa" and s.title not in progress:
                n = len(section_units(s))
                front.append(Piece(s, 0, n, n))
    # The book title belongs to the file only once, at its very top.
    need_h1 = include_front and bool(h1) and H1_MARK not in (
        dest.read_text(encoding="utf-8") if dest.exists() else ""
    )

    to_translate = front + portion
    chars = sum(p.chars for p in to_translate)
    main_total = sum(units_chars(section_units(s)) for s in sections if s.is_main_story) or 1
    main_share = sum(p.chars for p in portion) / main_total * 100

    inp_est, out_est = estimate_tokens(chars + (len(h1) if need_h1 else 0))
    usd = estimate_usd(model_id, inp_est, out_est, args.batch)

    print("=== book translate estimate ===", flush=True)
    print(f"Source:  {source}", flush=True)
    print(f"Target:  {dest.name}  ({src_lang} -> {tgt_lang})", flush=True)
    print(f"Model:   {model_id}  batch={args.batch}  wrap_width={wrap_width}", flush=True)
    print(
        f"Portion: ~{main_share:.1f}% of main content (request {args.portion}%, "
        f"window {args.portion*PORTION_LO:.0f}-{args.portion*PORTION_HI:.0f}%, "
        f"min {MIN_PORTION_CHARS} chars), cut at {cut_note}",
        flush=True,
    )
    print("Sections:", flush=True)
    for p in to_translate:
        where = ""
        if not (p.is_section_start and p.is_section_end):
            where = f", sentences {p.start + 1}-{p.end} of {p.total_units}"
            if p.is_section_end:
                where += " (completes it)"
        print(f"  - {p.section.title}  ({p.chars} chars{where})", flush=True)
    if need_h1:
        print(f"  - [H1] {h1.replace(chr(10), ' / ')}", flush=True)
    print(f"Chars≈{chars}; est tokens in≈{inp_est} out≈{out_est}; est USD≈${usd:.3f}", flush=True)
    if args.apply_batch:
        print(
            f"Apply mode: will pull ended batch {args.apply_batch} "
            "(no new generation charge) and append as this portion.",
            flush=True,
        )

    if args.estimate_only:
        return 0

    if not args.yes:
        try:
            prompt = (
                "Proceed with applying this batch? [y/N] "
                if args.apply_batch
                else "Proceed with translation? [y/N] "
            )
            ans = input(prompt).strip().lower()
        except EOFError:
            ans = "n"
        if ans not in {"y", "yes"}:
            print("Aborted.", flush=True)
            return 1

    client, system = make_client_and_system(src_lang, tgt_lang)
    if client is None:
        return 2

    # Every piece is split into request-sized chunks so that no single answer
    # can run into the token limit and come back cut off mid-word.
    stamp = int(time.time())
    items: list[tuple[str, str]] = []
    owner: list[int] = []  # index into to_translate; -1 = H1
    expected_slots: list[tuple[int, int]] = []
    if need_h1:
        items.append((f"{stamp}-h1", fragment_user_message(h1, [])))
        owner.append(-1)
        expected_slots.append((-1, 0))
    for i, piece in enumerate(to_translate):
        for j, user in enumerate(piece_requests(piece)):
            items.append((f"{stamp}-p{i}-{j}", user))
            owner.append(i)
            expected_slots.append((i, j))
    print(f"Requests: {len(items)}", flush=True)

    if args.apply_batch:
        texts_map, usage = fetch_ended_batch_results(client, args.apply_batch)
        texts = texts_for_slots(texts_map, expected_slots)
    else:
        usage, texts = run_requests(client, model_id, system, items, args.batch)

    joined: dict[int, str] = {}
    for idx, text in zip(owner, texts):
        joined[idx] = (joined.get(idx, "") + "\n\n" + text.strip()).strip()
    blocks: list[tuple[str, str]] = []
    if need_h1:
        blocks.append((H1_MARK, joined[-1]))
    for i, piece in enumerate(to_translate):
        blocks.append((src_mark(piece.section.title, piece.end, piece.total_units), joined[i]))

    append_translated_blocks(dest, blocks, wrap_width)
    pin, pout = PRICE_IN_OUT[model_id]
    actual = (usage["input_tokens"] / 1e6) * pin + (usage["output_tokens"] / 1e6) * pout
    if args.batch or args.apply_batch:
        actual *= BATCH_DISCOUNT
    print(
        f"Saved {dest}  tokens in={usage['input_tokens']} out={usage['output_tokens']} "
        f"actual_usd~=${actual:.3f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
