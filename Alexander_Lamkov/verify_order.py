#!/usr/bin/env python3
import json, re, subprocess, sys
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

def pl_order(pl_id):
    proc = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-json", f"https://www.youtube.com/playlist?list={pl_id}"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    o = {}
    for line in proc.stdout.splitlines():
        if line.strip():
            e = json.loads(line)
            o[e["id"]] = int(e.get("playlist_index") or e.get("playlist_autonumber"))
    return o

for folder, pl_id in PLAYLIST_IDS.items():
    po = pl_order(pl_id)
    files = list((ROOT / folder).glob("*.mp4"))
    by_name = sorted(files, key=lambda f: f.name.lower())
    by_pl = sorted(files, key=lambda f: po[VIDEO_ID_RE.search(f.name).group(1)])
    ok = [f.name for f in by_name] == [f.name for f in by_pl]
    print(f"{folder}: {'OK' if ok else 'FAIL'} ({len(files)} files)")
