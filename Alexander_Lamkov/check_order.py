#!/usr/bin/env python3
"""Analyze whether file mtime order matches YouTube playlist order."""

import json
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
VIDEO_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.mp4$")

PLAYLIST_IDS = {
    "html_kurs_2025": "PL0MUAHwery4ot0KmgGxlBSB7rXssLeA6h",
    "css_kurs_2025": "PL0MUAHwery4o9I7QQVj_RP4ZVpmdx6evz",
    "javascript_kurs_2025": "PL0MUAHwery4qn4Y27iUxmzC-JiauX7vSL",
    "react_kurs_2025": "PL0MUAHwery4omH4GyVQ-lI2R326tOdN7A",
    "adaptivnaya_verstka_html_css_figma": "PL0MUAHwery4rqkzKF1mDBCIH_eZgjY6uN",
    "accessibility_kurs_2025": "PL0MUAHwery4r4gCA3AOtHgArM_UOb2QUV",
    "verstka_saytov_master_klassy": "PL0MUAHwery4pP3XMpzDIMirWRS28ffD_x",
}


def yt_dlp_flat_json(url: str) -> list[dict]:
    proc = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-json", url],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def analyze_folder(folder: str) -> None:
    pl_id = PLAYLIST_IDS.get(folder)
    if not pl_id:
        print(f"\n=== {folder}: unknown playlist ===")
        return

    url = f"https://www.youtube.com/playlist?list={pl_id}"
    entries = yt_dlp_flat_json(url)
    pl_order = {e["id"]: e.get("playlist_index", 0) for e in entries}

    folder_path = ROOT / folder
    files = list(folder_path.glob("*.mp4"))
    file_data = []
    for f in files:
        m = VIDEO_ID_RE.search(f.name)
        if m:
            vid = m.group(1)
            file_data.append({
                "file": f.name[:70],
                "vid": vid,
                "pl_index": pl_order.get(vid, 999),
                "mtime": f.stat().st_mtime,
            })

    by_pl = sorted(file_data, key=lambda x: x["pl_index"])
    by_mtime = sorted(file_data, key=lambda x: x["mtime"])

    pl_ids = [x["vid"] for x in by_pl]
    mt_ids = [x["vid"] for x in by_mtime]
    match = pl_ids == mt_ids

    print(f"\n=== {folder} ({len(files)} files) ===")
    print(f"  Playlist order == mtime order: {'YES' if match else 'NO'}")

    if not match:
        mismatches = sum(1 for p, m in zip(by_pl, by_mtime) if p["vid"] != m["vid"])
        print(f"  Mismatched positions: {mismatches}/{len(by_pl)}")
        shown = 0
        for i, (p, m) in enumerate(zip(by_pl, by_mtime)):
            if p["vid"] != m["vid"]:
                print(f"    pos {i+1}: pl#{p['pl_index']} vs mtime")
                print(f"      pl: {p['file']}")
                print(f"      mt: {m['file']}")
                shown += 1
                if shown >= 5:
                    break


for folder in PLAYLIST_IDS:
    analyze_folder(folder)
