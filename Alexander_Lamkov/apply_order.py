#!/usr/bin/env python3
"""Apply playlist ordering to video filenames (hybrid strategy)."""

import json
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
VIDEO_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.mp4$")
LEADING_N_DOT = re.compile(r"^(\d)\.(\s)")
LEADING_N_DOT_OK = re.compile(r"^\d{2,}\.\s")
LEADING_HASH_N = re.compile(r"^#(\d)(\s)")

PLAYLIST_IDS = {
    "html_kurs_2025": "PL0MUAHwery4ot0KmgGxlBSB7rXssLeA6h",
    "css_kurs_2025": "PL0MUAHwery4o9I7QQVj_RP4ZVpmdx6evz",
    "javascript_kurs_2025": "PL0MUAHwery4qn4Y27iUxmzC-JiauX7vSL",
    "react_kurs_2025": "PL0MUAHwery4omH4GyVQ-lI2R326tOdN7A",
    "adaptivnaya_verstka_html_css_figma": "PL0MUAHwery4rqkzKF1mDBCIH_eZgjY6uN",
    "accessibility_kurs_2025": "PL0MUAHwery4r4gCA3AOtHgArM_UOb2QUV",
    "verstka_saytov_master_klassy": "PL0MUAHwery4pP3XMpzDIMirWRS28ffD_x",
}

# Pad existing N. / #N at start; add prefix only when no leading order marker
PAD_ONLY = {"html_kurs_2025", "css_kurs_2025", "adaptivnaya_verstka_html_css_figma"}
PREFIX_ONLY = {
    "javascript_kurs_2025",
    "react_kurs_2025",
    "accessibility_kurs_2025",
    "verstka_saytov_master_klassy",
}


def yt_dlp_playlist_order(pl_id: str) -> dict[str, int]:
    url = f"https://www.youtube.com/playlist?list={pl_id}"
    proc = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-json", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    order: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        idx = entry.get("playlist_index") or entry.get("playlist_autonumber")
        if idx is not None:
            order[entry["id"]] = int(idx)
    return order


def extract_video_id(name: str) -> str | None:
    m = VIDEO_ID_RE.search(name)
    return m.group(1) if m else None


def pad_leading_number(name: str) -> str | None:
    m = LEADING_N_DOT.match(name)
    if m:
        return f"0{m.group(1)}.{m.group(2)}" + name[m.end() :]
    m = LEADING_HASH_N.match(name)
    if m:
        return f"#0{m.group(1)}{m.group(2)}" + name[m.end() :]
    return None


def prefix_name(name: str, index: int) -> str:
    prefix = f"{index:02d}_"
    if name.startswith(prefix):
        return name
    return prefix + name


def plan_rename(folder: str, pl_order: dict[str, int]) -> list[tuple[Path, Path]]:
    folder_path = ROOT / folder
    plans: list[tuple[Path, Path]] = []

    for f in sorted(folder_path.glob("*.mp4")):
        vid = extract_video_id(f.name)
        if not vid or vid not in pl_order:
            print(f"  WARN: no playlist index for {f.name}")
            continue

        idx = pl_order[vid]
        new_name = f.name

        if folder in PAD_ONLY:
            padded = pad_leading_number(new_name)
            if padded:
                new_name = padded
            elif LEADING_N_DOT_OK.match(new_name) or LEADING_HASH_N.match(new_name.replace("#0", "#")):
                pass  # already 10.+ or #10+ — leave unchanged
            else:
                new_name = prefix_name(new_name, idx)
        elif folder in PREFIX_ONLY:
            new_name = prefix_name(new_name, idx)
        else:
            padded = pad_leading_number(new_name)
            new_name = padded if padded else prefix_name(new_name, idx)

        if new_name != f.name:
            plans.append((f, folder_path / new_name))

    return plans


def apply_renames(plans: list[tuple[Path, Path]]) -> None:
    # Two-phase rename to avoid collisions (e.g. 1.x <-> 01.x)
    temp: list[tuple[Path, Path]] = []
    for i, (src, dst) in enumerate(plans):
        tmp = src.parent / f"__tmp_order_{i:04d}__.mp4"
        src.rename(tmp)
        temp.append((tmp, dst))

    for tmp, dst in temp:
        if dst.exists():
            raise FileExistsError(f"Target already exists: {dst.name}")
        tmp.rename(dst)


def main() -> None:
    all_plans: list[tuple[str, list[tuple[Path, Path]]]] = []

    for folder in PLAYLIST_IDS:
        print(f"Planning: {folder}")
        pl_order = yt_dlp_playlist_order(PLAYLIST_IDS[folder])
        plans = plan_rename(folder, pl_order)
        print(f"  {len(plans)} renames")
        all_plans.append((folder, plans))

    total = sum(len(p) for _, p in all_plans)
    print(f"\nTotal renames: {total}")

    log: dict[str, list[dict]] = {}
    for folder, plans in all_plans:
        if not plans:
            continue
        print(f"\nApplying: {folder}")
        apply_renames(plans)
        log[folder] = [{"from": a.name, "to": b.name} for a, b in plans]

    report = ROOT / "_order_renames.json"
    report.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. Log: {report.name}")


if __name__ == "__main__":
    main()
