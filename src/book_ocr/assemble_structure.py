"""
Structure-driven book assembly for large books with a two-level TOC
(Part → Chapter). Used by assemble_book.py when assemble.settings.txt
is present under the book folder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from assemble_book import (
    Section,
    clean_paragraph,
    ends_with_hyphenated_break,
    join_across_break,
)


@dataclass
class ChapterSpec:
    part: int
    start: int
    title: str


@dataclass
class PartSpec:
    number: int
    start: int
    title: str
    chapters: list[ChapterSpec] = field(default_factory=list)


@dataclass
class BookStructure:
    lang: str
    title: str
    toc_title: str
    wrap_width: int
    split_by_parts: str  # auto | yes | no
    part_page_threshold: int
    front_end: int
    back_start: int | None
    back_title: str
    parts: list[PartSpec]
    back_chapters: list[ChapterSpec] = field(default_factory=list)

    def should_split(self, page_count: int) -> bool:
        mode = self.split_by_parts.lower()
        if mode == "yes":
            return True
        if mode == "no":
            return False
        return bool(self.parts) and page_count > self.part_page_threshold


def _parse_pipe_row(value: str) -> list[str]:
    return [p.strip() for p in value.split("|")]


def load_structure(path: Path) -> BookStructure:
    data: dict[str, str] = {}
    parts_raw: list[tuple[int, int, str]] = []
    chapters_raw: list[tuple[int, int, str]] = []

    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if key == "PART":
            n, start, title = _parse_pipe_row(value)
            parts_raw.append((int(n), int(start), title))
        elif key == "CHAPTER":
            n, start, title = _parse_pipe_row(value)
            chapters_raw.append((int(n), int(start), title))
        elif key not in data:
            data[key] = value

    parts = [
        PartSpec(number=n, start=start, title=title)
        for n, start, title in sorted(parts_raw, key=lambda x: x[0])
    ]
    by_part = {p.number: p for p in parts}
    back_chapters: list[ChapterSpec] = []
    max_part = max((p.number for p in parts), default=0)
    for n, start, title in sorted(chapters_raw, key=lambda x: (x[0], x[1])):
        ch = ChapterSpec(part=n, start=start, title=title)
        if n in by_part:
            by_part[n].chapters.append(ch)
        elif n == max_part + 1:
            back_chapters.append(ch)
        else:
            raise ValueError(f"CHAPTER part {n} has no matching PART in {path}")

    back_start = data.get("BACK_START")
    return BookStructure(
        lang=data.get("LANG", "RU"),
        title=data.get("TITLE", path.parent.name),
        toc_title=data.get("TOC_TITLE", "Содержание"),
        wrap_width=int(data.get("WRAP_WIDTH", "80")),
        split_by_parts=data.get("SPLIT_BY_PARTS", "auto"),
        part_page_threshold=int(data.get("PART_PAGE_THRESHOLD", "500")),
        front_end=int(data.get("FRONT_END", "0")),
        back_start=int(back_start) if back_start else None,
        back_title=data.get("BACK_TITLE", "Приложения"),
        parts=parts,
        back_chapters=back_chapters,
    )


def discover_page_files(text_root: Path) -> list[tuple[int, Path]]:
    pages: list[tuple[int, Path]] = []
    for path in text_root.glob("page_*.md"):
        m = re.fullmatch(r"page_(\d+)", path.stem)
        if m:
            pages.append((int(m.group(1)), path))
    pages.sort(key=lambda x: x[0])
    return pages


def page_body_lines(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8")
    body = re.sub(r"(?m)^<!--.*?-->\s*", "", raw)
    m = re.search(r"(?m)^## .+\n", body)
    if m:
        body = body[m.end() :]
    return [ln.rstrip() for ln in body.splitlines()]


def _norm_header(s: str) -> str:
    s = s.casefold()
    s = s.replace("ё", "е")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^\w\s.]", "", s, flags=re.UNICODE)
    return s


def is_running_header(line: str, headers: set[str]) -> bool:
    t = line.strip()
    if not t:
        return False
    if re.fullmatch(r"\d{1,4}", t):
        return True
    nt = _norm_header(t)
    for h in headers:
        nh = _norm_header(h)
        if not nh:
            continue
        if nt == nh or nh.startswith(nt) or nt.startswith(nh[:24]):
            return True
        # Short running header like "3 Решение проблем" vs full chapter title
        if len(nt) >= 12 and (nt in nh or nh in nt):
            return True
    return False


def dehyphenate_and_flow(lines: list[str]) -> list[str]:
    """Join soft hyphenation; keep blank lines as paragraph breaks;
    otherwise merge into flowing paragraphs."""
    logical: list[str] = []
    buf = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            if buf:
                logical.append(clean_paragraph(buf))
                buf = ""
            continue
        if buf and ends_with_hyphenated_break(buf):
            buf = join_across_break(buf, line)
            continue
        if not buf:
            buf = line
            continue
        # New paragraph if previous looks finished and this looks like a start
        prev_end = bool(re.search(r'[.!?…:»"”]\s*$', buf))
        new_start = bool(re.match(r"^[A-ZА-ЯЁ«\"“(0-9]", line))
        short_prev = len(buf) < 50
        if prev_end and new_start and not short_prev:
            logical.append(clean_paragraph(buf))
            buf = line
        else:
            buf = buf + " " + line
    if buf:
        logical.append(clean_paragraph(buf))
    return [p for p in logical if p]


def extract_page_paragraphs(path: Path, headers: set[str]) -> list[str]:
    lines = [
        ln
        for ln in page_body_lines(path)
        if ln.strip() and not is_running_header(ln, headers)
    ]
    return [strip_xml_controls(p) for p in dehyphenate_and_flow(lines)]


def strip_xml_controls(text: str) -> str:
    """Remove C0 controls that python-docx/lxml reject (keep \\t \\n \\r)."""
    return "".join(
        ch for ch in text if ord(ch) >= 32 or ch in "\t\n\r"
    )


def front_matter_sections(
    pages: list[tuple[int, Path]],
    front_end: int,
    headers: set[str],
) -> list[Section]:
    """Group early pages under simple headings when the first body line looks
    like a title (Предисловие, Об авторах, …). Skip full-book contents pages."""
    toc_names = {"содержание", "оглавление"}
    sections: list[Section] = []
    current: Section | None = None
    in_toc = False

    title_re = re.compile(
        r"^(Предисловие|Об авторах|Оглавление|Содержание)\b",
        re.I,
    )

    for num, path in pages:
        if num > front_end:
            break
        raw_lines = [
            ln.strip()
            for ln in page_body_lines(path)
            if ln.strip() and not is_running_header(ln, headers)
        ]
        if not raw_lines:
            continue

        first_line = raw_lines[0]
        if in_toc:
            # Still inside the book-wide contents block until a real front heading.
            if title_re.match(first_line) and first_line.casefold().split(".")[0].strip() not in toc_names:
                in_toc = False
                # fall through and open the new section below via re-check
            else:
                continue

        if title_re.match(first_line):
            title = first_line.split(".")[0].strip()
            # Strip a trailing printed page number accidentally left in the title.
            title = re.sub(r"\s+\d{1,4}$", "", title).strip()
            if title.casefold() in toc_names:
                in_toc = True
                current = None
                continue
            in_toc = False
            body_lines = raw_lines[1:]
            if current is not None and current.title.casefold() == title.casefold():
                # Running header restating the same front section.
                current.paragraphs.extend(dehyphenate_and_flow(body_lines))
                continue
            current = Section(title, dehyphenate_and_flow(body_lines))
            sections.append(current)
            continue

        paras = dehyphenate_and_flow(raw_lines)
        paras = [strip_xml_controls(p) for p in paras]
        if current is None:
            current = Section("Титул", [])
            sections.append(current)
        current.paragraphs.extend(paras)

    # Final scrub
    for sec in sections:
        sec.paragraphs = [strip_xml_controls(p) for p in sec.paragraphs if p.strip()]
    return [s for s in sections if s.title.casefold() not in toc_names]


def build_volume(
    *,
    h1: str,
    toc_title: str,
    wrap_width: int,
    volume_title: str,
    chapters: list[ChapterSpec],
    page_files: dict[int, Path],
    page_start: int,
    page_end: int,
    prepend: list[Section] | None = None,
    skip_toc_pages: bool = True,
) -> tuple[str, list[Section], int]:
    """Build one OUTPUT volume. Returns (h1, sections, wrap_width)."""
    headers = {volume_title, *(c.title for c in chapters)}
    # Also accept shortened chapter running headers ("Глава N. …" → keep full)
    for c in chapters:
        m = re.match(r"(Глава\s+\d+)\.", c.title)
        if m:
            headers.add(m.group(1))

    sections: list[Section] = list(prepend or [])
    toc_index = len(sections)
    sections.append(Section(toc_title, []))

    if not chapters:
        # Whole range as one body section
        body = Section(volume_title, [])
        for num in range(page_start, page_end + 1):
            path = page_files.get(num)
            if not path:
                continue
            if skip_toc_pages and num < page_start:
                continue
            body.paragraphs.extend(extract_page_paragraphs(path, headers))
        sections.append(body)
    else:
        bounds = list(chapters)
        for i, ch in enumerate(bounds):
            end = (bounds[i + 1].start - 1) if i + 1 < len(bounds) else page_end
            start = max(ch.start, page_start)
            sec = Section(ch.title, [])
            for num in range(start, end + 1):
                path = page_files.get(num)
                if not path:
                    continue
                paras = extract_page_paragraphs(path, headers | {ch.title})
                # Drop a leading paragraph that restates the chapter title
                if paras and _norm_header(paras[0]) == _norm_header(ch.title):
                    paras = paras[1:]
                sec.paragraphs.extend(paras)
            sections.append(sec)

    # Part divider page (e.g. 33 "Часть I") before first chapter: attach any
    # leftover pages in [page_start, first_chapter) as a short lead-in.
    if chapters and page_start < chapters[0].start:
        lead = Section(volume_title, [])
        for num in range(page_start, chapters[0].start):
            path = page_files.get(num)
            if path:
                lead.paragraphs.extend(extract_page_paragraphs(path, headers))
        if lead.paragraphs:
            sections.insert(toc_index + 1, lead)

    toc_lines = [
        s.title
        for i, s in enumerate(sections)
        if i != toc_index and s.title != toc_title
    ]
    sections[toc_index].paragraphs = toc_lines
    vol_h1 = f"{h1}\n{volume_title}" if volume_title not in h1 else h1
    return vol_h1, sections, wrap_width


def iter_volumes(
    structure: BookStructure,
    text_root: Path,
) -> list[tuple[int, str, list[Section], int]]:
    """Return list of (volume_number, h1, sections, wrap_width)."""
    discovered = discover_page_files(text_root)
    if not discovered:
        raise FileNotFoundError(f"no page_*.md in {text_root}")
    page_files = dict(discovered)
    last_page = discovered[-1][0]
    h1 = structure.title.replace(" / ", "\n")

    volumes: list[tuple[int, str, list[Section], int]] = []
    front_headers = {
        *(p.title for p in structure.parts),
    }

    for i, part in enumerate(structure.parts):
        next_start = (
            structure.parts[i + 1].start
            if i + 1 < len(structure.parts)
            else (structure.back_start or (last_page + 1))
        )
        page_end = next_start - 1
        prepend: list[Section] = []
        if i == 0 and structure.front_end > 0:
            prepend = front_matter_sections(
                discovered, structure.front_end, front_headers
            )

        vol_h1, sections, width = build_volume(
            h1=h1,
            toc_title=structure.toc_title,
            wrap_width=structure.wrap_width,
            volume_title=part.title,
            chapters=part.chapters,
            page_files=page_files,
            page_start=part.start,
            page_end=page_end,
            prepend=prepend,
        )
        volumes.append((part.number, vol_h1, sections, width))

    if structure.back_start and structure.back_start <= last_page:
        back_num = (structure.parts[-1].number + 1) if structure.parts else 1
        vol_h1, sections, width = build_volume(
            h1=h1,
            toc_title=structure.toc_title,
            wrap_width=structure.wrap_width,
            volume_title=structure.back_title,
            chapters=structure.back_chapters,
            page_files=page_files,
            page_start=structure.back_start,
            page_end=last_page,
        )
        volumes.append((back_num, vol_h1, sections, width))

    return volumes
