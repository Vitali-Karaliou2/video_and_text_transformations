"""
Translate a assembled book (OUTPUT/Book_XX.*) into another language in portions.

Source language is taken from the last two letters of the Book_XX stem.
Target language is --to (e.g. RU). Progressive runs translate the next ~portion
of main-story content (default 10%, accepted as 8–12% by whole stories).

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
BLOCK_MARK_RE = re.compile(r"(?m)^<!--\s*src-(?:h1|title:\s*(.+?))\s*-->[ \t]*$")


def src_mark(title: str) -> str:
    return f"<!-- src-title: {title} -->"


def split_marked_blocks(text: str) -> tuple[str, list[tuple[str | None, str]]]:
    """Split a working file into (header, [(source title | None for H1, body)])."""
    marks = list(BLOCK_MARK_RE.finditer(text))
    if not marks:
        return text, []
    header = text[: marks[0].start()].strip("\n")
    blocks: list[tuple[str | None, str]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        blocks.append((m.group(1), text[m.end() : end].strip("\n")))
    return header, blocks


def translated_source_titles(dest: Path) -> set[str]:
    if not dest.exists():
        return set()
    _header, blocks = split_marked_blocks(dest.read_text(encoding="utf-8"))
    return {title for title, _body in blocks if title}


def ensure_markers(dest: Path) -> bool:
    """True if the file can be tracked (is new, or already carries markers)."""
    if not dest.exists() or not dest.read_text(encoding="utf-8").strip():
        return True
    return bool(BLOCK_MARK_RE.search(dest.read_text(encoding="utf-8")))


def select_next_portion(
    sections: list[Section],
    done_titles: set[str],
    portion_pct: float,
) -> list[Section]:
    """Pick whole main stories until cumulative share is in [8%, 12%] of remaining? 

    Spec: each run translates ~portion of the *document* (default 10%), mapped to
    an interval portion*(0.8..1.2), choosing whole stories. We measure against
    total main-story words (not remaining), and skip already-done titles.
    """
    main = [s for s in sections if s.is_main_story]
    total = sum(s.word_count for s in main) or 1
    target = portion_pct / 100.0
    lo, hi = target * PORTION_LO, target * PORTION_HI

    pending = [s for s in main if s.title not in done_titles]
    if not pending:
        return []

    chosen: list[Section] = []
    cum = 0.0
    for s in pending:
        share = s.word_count / total
        # Always take at least one story.
        if chosen and cum + share > hi and cum >= lo:
            break
        chosen.append(s)
        cum += share
        if cum >= lo and cum <= hi:
            break
        if cum > hi:
            # Single oversized story: still take it alone.
            break
    return chosen


def estimate_tokens(sections: list[Section], h1: str, include_front: bool) -> tuple[int, int]:
    """Rough input/output token estimate for costing."""
    chars = len(h1)
    for s in sections:
        chars += len(s.title) + sum(len(p) for p in s.paragraphs) + 10
    # Polish ~3.5 chars/token; output RU similar + prompt overhead
    inp = int(chars / 3.5) + 800
    out = int(chars / 3.2) + 200
    return inp, out


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
    dest: Path, blocks: list[tuple[str | None, str]], wrap_width: int
) -> None:
    """Append translated blocks, each tagged with its source section marker."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    pieces: list[str] = []
    is_new = not dest.exists() or not dest.read_text(encoding="utf-8").strip()
    if is_new:
        pieces.append(
            f"<!-- translated by book_translate; wrap_width: {wrap_width}; "
            f"incomplete until all stories done -->"
        )
    else:
        pieces.append(dest.read_text(encoding="utf-8").rstrip())

    for title, raw in blocks:
        body = format_translated_markdown(raw, wrap_width).strip("\n")
        if not body:
            continue
        if title is None:
            # The book title must stay an H1 even if the model returned "## ".
            body = re.sub(r"^#+\s+", "# ", body)
            pieces.append(H1_MARK)
        else:
            pieces.append(src_mark(title))
        pieces.append(body)
    dest.write_text("\n\n".join(pieces) + "\n", encoding="utf-8")

def call_realtime(client, model_id: str, system: str, user: str) -> tuple[str, dict]:
    msg = client.messages.create(
        model=model_id,
        max_tokens=16384,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    usage = {
        "input_tokens": getattr(msg.usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(msg.usage, "output_tokens", 0) or 0,
    }
    return text.strip() + "\n", usage


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
                max_tokens=16384,
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

    texts: dict[str, str] = {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            raise RuntimeError(f"Batch item {result.custom_id} failed: {result.result}")
        msg = result.result.message
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        texts[result.custom_id] = text.strip() + "\n"
        usage["input_tokens"] += getattr(msg.usage, "input_tokens", 0) or 0
        usage["output_tokens"] += getattr(msg.usage, "output_tokens", 0) or 0
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
    return p.parse_args()


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
    done = translated_source_titles(dest)
    by_title = {s.title: s for s in sections}
    todo = [by_title[t] for t in args.redo_titles if t in done]
    skipped = wanted - unknown - done
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
    inp_est, out_est = estimate_tokens(todo, "", False)
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
    items = [(f"{stamp}-redo{i}", fragment_user_message(None, [sec])) for i, sec in enumerate(todo)]
    if args.batch:
        texts_map, usage = run_batch_multi(client, model_id, system, items)
        chunks = [texts_map[cid] for cid, _ in items]
    else:
        usage = {"input_tokens": 0, "output_tokens": 0}
        chunks = []
        for _cid, user in items:
            chunk, u = call_realtime(client, model_id, system, user)
            chunks.append(chunk)
            usage["input_tokens"] += u["input_tokens"]
            usage["output_tokens"] += u["output_tokens"]

    # Replace the marked blocks in place, keeping file order untouched.
    header, blocks = split_marked_blocks(dest.read_text(encoding="utf-8"))
    fresh = {sec.title: chunk for sec, chunk in zip(todo, chunks)}
    pieces = [header] if header else []
    for title, body in blocks:
        if title in fresh:
            body = format_translated_markdown(fresh[title], wrap_width).strip("\n")
        pieces.append(H1_MARK if title is None else src_mark(title))
        pieces.append(body)
    dest.write_text("\n\n".join(p for p in pieces if p) + "\n", encoding="utf-8")

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

    done = translated_source_titles(dest)
    portion_secs = select_next_portion(sections, done, args.portion)
    if not portion_secs:
        print("Nothing left to translate (all main stories present in target file).", flush=True)
        return 0

    include_front = args.include_front or (not dest.exists())
    front: list[Section] = []
    if include_front:
        for s in sections:
            if s.title == "Przedmowa" and s.title not in done:
                front.append(s)
    # The book title belongs to the file only once, at its very top.
    need_h1 = include_front and bool(h1) and H1_MARK not in (
        dest.read_text(encoding="utf-8") if dest.exists() else ""
    )

    to_translate = front + portion_secs
    words = sum(s.word_count for s in to_translate)
    main_total = sum(s.word_count for s in sections if s.is_main_story) or 1
    main_share = sum(s.word_count for s in portion_secs) / main_total * 100

    inp_est, out_est = estimate_tokens(to_translate, h1 if need_h1 else "", include_front)
    usd = estimate_usd(model_id, inp_est, out_est, args.batch)

    print("=== book translate estimate ===", flush=True)
    print(f"Source:  {source}", flush=True)
    print(f"Target:  {dest.name}  ({src_lang} -> {tgt_lang})", flush=True)
    print(f"Model:   {model_id}  batch={args.batch}  wrap_width={wrap_width}", flush=True)
    print(f"Portion: ~{main_share:.1f}% of main stories (request {args.portion}%, window {args.portion*PORTION_LO:.0f}-{args.portion*PORTION_HI:.0f}%)", flush=True)
    print("Sections:", flush=True)
    for s in to_translate:
        label = s.title or "(H1/preamble)"
        print(f"  - {label}  ({s.word_count} words)", flush=True)
    if need_h1:
        print(f"  - [H1] {h1.replace(chr(10), ' / ')}", flush=True)
    print(f"Words≈{words}; est tokens in≈{inp_est} out≈{out_est}; est USD≈${usd:.3f}", flush=True)

    if args.estimate_only:
        return 0

    if not args.yes:
        try:
            ans = input("Proceed with translation? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in {"y", "yes"}:
            print("Aborted.", flush=True)
            return 1

    client, system = make_client_and_system(src_lang, tgt_lang)
    if client is None:
        return 2

    # One API item per section (plus optional H1) so long stories do not hit max_tokens.
    stamp = int(time.time())
    items: list[tuple[str, str]] = []
    labels: list[str | None] = []  # source title per item; None = H1
    if need_h1:
        items.append((f"{stamp}-h1", fragment_user_message(h1, [])))
        labels.append(None)
    for i, sec in enumerate(to_translate):
        items.append((f"{stamp}-s{i}", fragment_user_message(None, [sec])))
        labels.append(sec.title)

    if args.batch:
        texts_map, usage = run_batch_multi(client, model_id, system, items)
        chunks = [texts_map[cid] for cid, _ in items]
    else:
        usage = {"input_tokens": 0, "output_tokens": 0}
        chunks = []
        for _cid, user in items:
            chunk, u = call_realtime(client, model_id, system, user)
            chunks.append(chunk)
            usage["input_tokens"] += u["input_tokens"]
            usage["output_tokens"] += u["output_tokens"]

    append_translated_blocks(dest, list(zip(labels, chunks)), wrap_width)
    pin, pout = PRICE_IN_OUT[model_id]
    actual = (usage["input_tokens"] / 1e6) * pin + (usage["output_tokens"] / 1e6) * pout
    if args.batch:
        actual *= BATCH_DISCOUNT
    print(
        f"Saved {dest}  tokens in={usage['input_tokens']} out={usage['output_tokens']} "
        f"actual_usd~=${actual:.3f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
