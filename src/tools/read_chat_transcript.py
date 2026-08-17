#!/usr/bin/env python3
"""Turn a chat transcript (.jsonl) into something a human can read.

A transcript is one JSON object per line - a whole conversation on a few
thousand very long lines, with the newlines of every command, file and reply
escaped as \\n. Two ways out, and the script does both:

- the default: one text file of about 80 columns, where every message is a
  titled block, the JSON of a tool call is printed with its braces, and a
  string that has newlines of its own is printed as the lines it really has
  (marked with `|`, the way YAML marks a literal block) instead of one
  escaped line;
- --split: a folder with one pretty-printed .json file per line of the
  transcript, which stays valid JSON and can be read by anything.

Where a line is broken is decided by what makes it readable, not by the
column count, so some lines are longer than the rest:

- markup (the <timestamp> / <user_query> wrapper of a user message, for
  instance) gets one tag per line, indented by nesting, and a tag with too
  many attributes gets one attribute per line;
- a command is folded at the pipes and semicolons outside its quotes - what
  runs it stays on the first line, every pipe after that opens a line of
  its own, and short pipes are grouped so as not to become a column of
  stubs;
- a path, a URL or any other word with nothing to break at is left whole,
  however long it is: the longest line of the result is as long as the
  longest path in it.

Examples:
  python src\\read_chat_transcript.py prompts\\2026_07_30_Chat.jsonl
  python src\\read_chat_transcript.py prompts\\2026_07_30_Chat.jsonl --split
  python src\\read_chat_transcript.py chat.jsonl --width 100 --out chat.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_WIDTH = 80
# A command is folded at its pipes and semicolons - the places a reader
# would break it anyway - so its lines are allowed to run a little longer.
COMMAND_WIDTH = 100
COMMAND_KEYS = {"command"}
INDENT = "  "
# A tag: '<' or '</' followed at once by a name, no '<' inside. Strict enough
# that "a < b" and "<=" in code are not mistaken for markup.
TAG = r"</?[A-Za-z_][\w.:-]*(?:\s[^<>]*?)?/?>"
TAG_RE = re.compile(TAG)
TAG_SPLIT_RE = re.compile(f"({TAG})")
TAG_PARTS_RE = re.compile(r"^<(/?)([\w.:-]+)([\s\S]*?)(/?)>$")
ATTR_RE = re.compile(r'[\w.:-]+\s*=\s*"[^"]*"')
# Nothing shorter is worth wrapping into.
MIN_WIDTH = 24


def has_tag_pair(text: str) -> bool:
    """Whether the text is markup: some tag in it is opened and closed.

    A tag merely mentioned in passing does not make a message markup, and a
    '<' of some code is not a tag at all.
    """
    opened: set[str] = set()
    closed: set[str] = set()
    for tag in TAG_RE.findall(text):
        if tag.startswith("</"):
            closed.add(tag[2:-1].strip())
        elif not tag.endswith("/>"):
            opened.add(tag[1:-1].split()[0])
    return bool(opened & closed)


def command_pieces(line: str) -> list[str]:
    """A command line cut at the pipes and semicolons outside its quotes.

    A pipe opens the piece that follows it, a semicolon closes the piece it
    ends - which is how both read.
    """
    pieces: list[str] = []
    current: list[str] = []
    quote = ""

    def flush() -> None:
        piece = "".join(current).strip()
        if piece:
            pieces.append(piece)
        current.clear()

    for char in line:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char == "|":
            flush()
            current.append(char)
        elif char == ";":
            current.append(char)
            flush()
        else:
            current.append(char)
    flush()
    return pieces


class Renderer:
    def __init__(self, width: int) -> None:
        self.width = width

    # ----------------------------------------------------------------- text

    def wrap(self, text: str, indent: str, width: int = 0) -> list[str]:
        """One paragraph, wrapped; words longer than the line stay whole."""
        room = max((width or self.width) - len(indent), MIN_WIDTH)
        return textwrap.wrap(
            text,
            width=room,
            initial_indent=indent,
            subsequent_indent=indent,
            break_long_words=False,
            break_on_hyphens=False,
        ) or [indent.rstrip()]

    def lines_of(self, text: str, indent: str) -> list[str]:
        """Text as it is written, with over-long lines wrapped."""
        out: list[str] = []
        for line in text.replace("\t", "    ").split("\n"):
            stripped = line.rstrip()
            if not stripped.strip():
                out.append("")
            elif len(indent) + len(stripped) <= self.width:
                out.append(indent + stripped)
            else:
                out.extend(self.wrap(stripped, indent))
        return out

    def tag(self, tag: str, indent: str) -> list[str]:
        """One tag; a tag too long for the line goes attribute per line."""
        if len(indent) + len(tag) <= self.width:
            return [indent + tag]
        parts = TAG_PARTS_RE.match(tag)
        if not parts:
            return self.wrap(tag, indent)
        slash, name, body, closing = parts.groups()
        attributes = ATTR_RE.findall(body)
        if not attributes:
            return self.wrap(tag, indent)
        out = [f"{indent}<{slash}{name}"]
        for attribute in attributes:
            out.extend(self.wrap(attribute, indent + INDENT))
        out.append(f"{indent}{closing}>")
        return out

    def markup(self, text: str, indent: str) -> list[str]:
        """Markup with one tag per line and the nesting shown by indent."""
        out: list[str] = []
        depth = 0
        for token in TAG_SPLIT_RE.split(text):
            if not token or not token.strip():
                continue
            if TAG_RE.fullmatch(token):
                if token.startswith("</"):
                    depth = max(depth - 1, 0)
                    out.extend(self.tag(token, indent + INDENT * depth))
                else:
                    out.extend(self.tag(token, indent + INDENT * depth))
                    if not token.endswith("/>"):
                        depth += 1
            else:
                out.extend(
                    self.lines_of(token.strip("\n"), indent + INDENT * depth)
                )
        return out

    def text_block(self, text: str, indent: str) -> list[str]:
        return (
            self.markup(text, indent)
            if has_tag_pair(text)
            else self.lines_of(text, indent)
        )

    # -------------------------------------------------------------- command

    def command(self, text: str, indent: str) -> list[str]:
        """A shell command, folded where a reader would fold it.

        The lines it really has are kept, and a line still too long is
        broken at its pipes and semicolons: what runs the command (an
        executable and the path it works on) stays on the first line, and
        every pipe that follows starts a line of its own - grouped, so that
        a chain of short pipes does not become a column of stubs.
        """
        out: list[str] = []
        for line in text.split("\n"):
            line = line.rstrip()
            if not line.strip():
                out.append("")
                continue
            if len(indent) + len(line) <= COMMAND_WIDTH:
                out.append(indent + line)
                continue
            groups: list[str] = []
            current = ""
            for piece in command_pieces(line):
                joined = f"{current} {piece}" if current else piece
                if current and len(indent) + len(joined) > COMMAND_WIDTH:
                    groups.append(current)
                    current = piece
                else:
                    current = joined
            if current:
                groups.append(current)
            for group in groups:
                # A piece with nothing to fold at - a quoted script, say -
                # is still worth breaking somewhere rather than running off
                # the page.
                if len(indent) + len(group) <= COMMAND_WIDTH:
                    out.append(indent + group)
                else:
                    out.extend(self.wrap(group, indent, COMMAND_WIDTH))
        return out

    # ----------------------------------------------------------------- json

    def value(
        self, value: object, indent: str, key: str | None = None
    ) -> tuple[list[str], bool]:
        """A JSON value and whether it ended as a block of raw text.

        A block takes no comma after it: the comma would read as part of
        the text.
        """
        head = indent + (f'"{key}": ' if key is not None else "")
        if isinstance(value, str):
            single = json.dumps(value, ensure_ascii=False)
            # One character is kept for the comma the caller may append.
            if "\n" not in value and len(head) + len(single) < self.width:
                return [head + single], False
            # Escaped, a command or a file would be one unreadable line. As
            # in YAML, '|' keeps the newlines the string has and '>' means
            # one line folded at a place that reads well.
            mark = "|" if "\n" in value else ">"
            body = (
                self.command(value, indent + INDENT)
                if key in COMMAND_KEYS
                else self.lines_of(value, indent + INDENT)
            )
            return [f"{head.rstrip()} {mark}"] + body, True
        if isinstance(value, dict):
            if not value:
                return [head + "{}"], False
            out = [head + "{"]
            items = list(value.items())
            for position, (name, item) in enumerate(items):
                lines, block = self.value(item, indent + INDENT, name)
                if position < len(items) - 1 and not block:
                    lines[-1] += ","
                out.extend(lines)
            out.append(indent + "}")
            return out, False
        if isinstance(value, list):
            if not value:
                return [head + "[]"], False
            out = [head + "["]
            for position, item in enumerate(value):
                lines, block = self.value(item, indent + INDENT)
                if position < len(value) - 1 and not block:
                    lines[-1] += ","
                out.extend(lines)
            out.append(indent + "]")
            return out, False
        return [head + json.dumps(value, ensure_ascii=False)], False

    def rendered(self, value: object, indent: str) -> list[str]:
        return self.value(value, indent)[0]

    # ------------------------------------------------------------- messages

    def event(self, number: int, total: int, entry: dict) -> list[str]:
        role = str(entry.get("role") or entry.get("type") or "?").upper()
        rule = "=" if role == "USER" else "-"
        out = [
            rule * self.width,
            f"[{number}/{total}] {role}",
            rule * self.width,
            "",
        ]
        if not isinstance(entry.get("message"), dict):
            # A turn marker: no content of its own, only its own fields.
            out.extend(
                self.rendered(
                    {k: v for k, v in entry.items() if k != "message"}, ""
                )
            )
            out.append("")
            return out
        for block in entry["message"].get("content") or []:
            kind = block.get("type")
            if kind == "text":
                out.extend(self.text_block(block.get("text") or "", ""))
            elif kind == "tool_use":
                out.append(f"--- {block.get('name')} " + "-" * 8)
                out.extend(self.rendered(block.get("input"), ""))
            else:
                out.append(f"--- {kind} " + "-" * 8)
                out.extend(self.rendered(block, ""))
            out.append("")
        return out


def header(source: Path, count: int, width: int) -> list[str]:
    paragraphs = (
        f"{count} message(s), rendered for reading at {width} "
        "columns. This is a copy to read, not to parse: a string "
        "too long for one line is printed as raw text after a mark "
        "- `|` when it has newlines of its own, `>` when it is one "
        "line folded - and such a block is not followed by the "
        "comma of the JSON it came from.",
        "Lines are broken where a reader would break them, so some "
        f"run past {width}: a command is folded at its pipes and "
        f"semicolons and may reach {COMMAND_WIDTH} columns, and a "
        "path, a URL or any other word with nothing to break at is "
        "left whole however long it is.",
    )
    lines = ["=" * width, f"Chat transcript: {source.name}", "=" * width]
    for paragraph in paragraphs:
        lines.append("")
        lines.extend(textwrap.wrap(paragraph, width=width))
    lines.append("")
    return lines


def render_file(source: Path, target: Path, width: int) -> int:
    entries = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    renderer = Renderer(width)
    lines = header(source, len(entries), width)
    for number, entry in enumerate(entries, start=1):
        lines.extend(renderer.event(number, len(entries), entry))
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return len(entries)


def split_file(source: Path, target_dir: Path) -> int:
    entries = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    target_dir.mkdir(parents=True, exist_ok=True)
    for number, entry in enumerate(entries, start=1):
        role = str(entry.get("role") or entry.get("type") or "event")
        path = target_dir / f"{number:04d}_{role}.json"
        path.write_text(
            json.dumps(entry, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return len(entries)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Make a chat transcript (.jsonl) readable."
    )
    parser.add_argument("transcript", type=Path, help="The .jsonl file")
    parser.add_argument(
        "--split",
        action="store_true",
        help=(
            "Write one pretty-printed .json per message into a folder named "
            "after the transcript, instead of the single text file"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="PATH",
        help="Where to write (default: next to the transcript)",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_WIDTH,
        metavar="N",
        help=f"Line width of the text file (default: {DEFAULT_WIDTH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.transcript
    if not source.is_file():
        raise SystemExit(f"Transcript not found: {source}")

    if args.split:
        target = args.out or source.with_suffix("")
        count = split_file(source, target)
        print(f"{count} message(s) -> {target}\\*.json", flush=True)
    else:
        target = args.out or source.with_suffix(".txt")
        count = render_file(source, target, max(args.width, MIN_WIDTH * 2))
        size = target.stat().st_size / 1024
        print(f"{count} message(s) -> {target} ({size:.0f} KB)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
