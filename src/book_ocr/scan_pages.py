"""
Scan Archive.org BookReader pages listed in pages_expanded.txt into pages_extracted/.

Requires Microsoft Edge started with remote debugging, e.g. (from the workspace root):
  powershell -File _run_scripts\\book_start_edge_for_scan.ps1

Then:
  python src\\book_ocr\\scan_pages.py
  python src\\book_ocr\\scan_pages.py --start-from page_012   # resume
  python src\\book_ocr\\scan_pages.py --limit 3               # dry run first pages

Data folders default to _books/Wojna_Futbolowa/ (see --book).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from book_paths import DEFAULT_BOOK, img_dir, pages_file

CDP_URL = "http://127.0.0.1:9222"
BOOK_HOST = "archive.org"

BLOCK_MARKERS = (
    "Borrow Unavailable",
    "Another patron is using this book",
    "Log in and Borrow",
    "Borrow for 1 hour",
    "Borrow for 14 days",
)


def load_urls(path: Path) -> list[str]:
    urls: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or set(line) <= {".", " "}:
            continue
        urls.append(line)
    return urls


def slug_from_url(url: str) -> str:
    m = re.search(r"/page/([^/]+)/", url)
    if m:
        leaf = m.group(1)
        if leaf.isdigit():
            return f"page_{int(leaf):03d}"
        return f"page_{leaf}"
    if url.rstrip("/").endswith("/mode/2up") or "/details/" in url:
        return "page_000_cover"
    return "page_unknown"


def find_book_page(context):
    for page in context.pages:
        if BOOK_HOST in page.url and "isbn_9788307026183" in page.url:
            return page
    for page in context.pages:
        if BOOK_HOST in page.url:
            return page
    return context.pages[0] if context.pages else None


def access_blocked(page) -> str | None:
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return None
    for marker in BLOCK_MARKERS:
        if marker in body:
            return marker
    return None


def wait_for_book_images(page, timeout_ms: int = 45000) -> None:
    # BookReader may use <img class="BRpageimage"> or canvas tiles.
    page.wait_for_function(
        """
        () => {
          const imgs = [...document.querySelectorAll('img.BRpageimage, .BRpagecontainer img, #BookReader img')];
          const visible = imgs.filter(i => i.offsetParent !== null && i.naturalWidth > 80);
          if (visible.length >= 1) return true;
          const canvas = document.querySelector('#BookReader canvas, .BRcontainer canvas');
          return !!(canvas && canvas.width > 80);
        }
        """,
        timeout=timeout_ms,
    )
    # Let tiles finish painting.
    page.wait_for_timeout(400)


def goto_with_retries(page, url: str, attempts: int = 3) -> None:
    last_exc: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            return
        except Exception as exc:
            last_exc = exc
            print(f"  WARN: goto failed ({i}/{attempts}): {exc}")
            page.wait_for_timeout(2000 * i)
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
    assert last_exc is not None
    raise last_exc


def screenshot_reader(page, dest: Path) -> None:
    selectors = (
        "#BookReader",
        ".BookReader",
        "#IABookReaderWrapper",
        ".BRcontainer",
        "[id*='BookReader']",
    )
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                loc.screenshot(path=str(dest), type="png")
                return
        except Exception:
            continue
    page.screenshot(path=str(dest), type="png", full_page=False)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scan borrowed Archive.org book pages via Edge CDP")
    p.add_argument("--book", default=DEFAULT_BOOK, help="Book folder name under _books/")
    p.add_argument("--cdp", default=CDP_URL, help="Edge remote debugging URL")
    p.add_argument("--pages-file", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--start-from", default=None, help="Skip until this slug, e.g. page_012")
    p.add_argument("--limit", type=int, default=None, help="Process only N pages (for testing)")
    p.add_argument("--delay", type=float, default=0.6, help="Pause between pages (seconds)")
    p.add_argument("--overwrite", action="store_true", help="Re-capture existing screenshots")
    args = p.parse_args()
    if args.pages_file is None:
        args.pages_file = pages_file(args.book)
    if args.out_dir is None:
        args.out_dir = img_dir(args.book)
    return args


def main() -> int:
    args = parse_args()
    urls = load_urls(args.pages_file)
    if not urls:
        print(f"No URLs in {args.pages_file}", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Connecting to Edge at {args.cdp} ...")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(args.cdp)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = find_book_page(context)
            if page is None:
                page = context.new_page()

            blocked = access_blocked(page)
            if blocked:
                print(
                    f"Access blocked ({blocked}). Make sure Borrow is active in Edge, then retry.",
                    file=sys.stderr,
                )
                return 2

            started = args.start_from is None
            done = 0
            skipped = 0
            total = len(urls)

            for idx, url in enumerate(urls, start=1):
                slug = slug_from_url(url)
                if not started:
                    if slug == args.start_from:
                        started = True
                    else:
                        skipped += 1
                        continue

                dest = args.out_dir / f"{slug}.png"
                if dest.exists() and not args.overwrite:
                    print(f"[{idx}/{total}] skip existing {dest.name}")
                    done += 1
                    if args.limit is not None and done >= args.limit:
                        break
                    continue

                print(f"[{idx}/{total}] {slug} <- {url}", flush=True)
                try:
                    goto_with_retries(page, url)
                except Exception as exc:
                    print(f"  SKIP after retries: {exc}", flush=True)
                    continue

                page.wait_for_timeout(500)

                blocked = access_blocked(page)
                if blocked:
                    print(f"  STOP: access blocked ({blocked})", file=sys.stderr)
                    return 2

                try:
                    wait_for_book_images(page)
                except PlaywrightTimeout:
                    print("  WARN: book images not detected in time; capturing anyway", flush=True)

                screenshot_reader(page, dest)
                size_kb = dest.stat().st_size / 1024
                print(f"  saved {dest.name} ({size_kb:.0f} KiB)", flush=True)
                done += 1
                time.sleep(args.delay)

                if args.limit is not None and done >= args.limit:
                    print(f"Limit {args.limit} reached.")
                    break

            print(f"Finished. Captured/kept={done}, skipped_before_start={skipped}, out={args.out_dir}")
            return 0
    except Exception as exc:
        msg = str(exc)
        if "ECONNREFUSED" in msg or "connect" in msg.lower():
            print(
                "Cannot connect to Edge debugging port.\n"
                "Close all Edge windows, run: powershell -File _run_scripts\\book_start_edge_for_scan.ps1,\n"
                "confirm pages are visible, then re-run: python src\\book_ocr\\scan_pages.py",
                file=sys.stderr,
            )
            return 3
        raise


if __name__ == "__main__":
    raise SystemExit(main())
