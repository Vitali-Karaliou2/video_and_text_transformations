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
    """Wait until EVERY visible page container has a loaded image.

    In 2-up mode the two pages of a spread load independently, sometimes with
    a long pause between them; waiting for just one image is not enough
    (that is how page_086 was captured with a not-yet-loaded left page).
    """
    page.wait_for_function(
        """
        () => {
          const containers = [...document.querySelectorAll('.BRpagecontainer')]
            .filter(c => c.offsetParent !== null && c.offsetWidth > 40);
          if (containers.length > 0) {
            return containers.every(c => {
              const img = c.querySelector('img');
              return img && img.complete && img.naturalWidth > 80;
            });
          }
          const imgs = [...document.querySelectorAll('img.BRpageimage, #BookReader img')];
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


def page_half_text_density(png_path: Path) -> list[float | None]:
    """Text contrast score for the paper area of each half of a screenshot.

    Finds the page (bright paper rectangle) in each half via brightness
    profiles, then returns median-minus-1st-percentile brightness inside it.
    Pages with text score ~60-100 regardless of scan contrast; blank or
    not-yet-loaded pages score ~0-15. None means no paper area was found.
    """
    from PIL import Image

    with Image.open(png_path) as im:
        gray = im.convert("L")
    w, h = gray.size
    px = gray.load()
    out: list[float | None] = []
    for x_off in (0, w // 2):
        hw = w // 2

        def bright_frac_col(x: int) -> float:
            n = b = 0
            for y in range(0, h, 4):
                n += 1
                if px[x_off + x, y] >= 185:
                    b += 1
            return b / max(n, 1)

        cols = [x for x in range(0, hw, 4) if bright_frac_col(x) > 0.35]
        if not cols:
            out.append(None)
            continue
        x0, x1 = min(cols), max(cols)

        def bright_frac_row(y: int) -> float:
            n = b = 0
            for x in range(x0, x1 + 1, 4):
                n += 1
                if px[x_off + x, y] >= 185:
                    b += 1
            return b / max(n, 1)

        rows = [y for y in range(0, h, 4) if bright_frac_row(y) > 0.5]
        if not rows:
            out.append(None)
            continue
        y0, y1 = min(rows), max(rows)
        # Shrink to skip page edges, shadows and gutter.
        dx = max(1, int((x1 - x0) * 0.08))
        dy = max(1, int((y1 - y0) * 0.08))
        x0, x1, y0, y1 = x0 + dx, x1 - dx, y0 + dy, y1 - dy
        if x1 - x0 < 20 or y1 - y0 < 20:
            out.append(None)
            continue
        vals = sorted(
            px[x_off + x, y] for y in range(y0, y1, 2) for x in range(x0, x1, 2)
        )
        if not vals:
            out.append(None)
            continue
        p1 = vals[len(vals) // 100]
        p50 = vals[len(vals) // 2]
        out.append(float(p50 - p1))
    return out


BLANK_SCORE = 25.0  # below this a page half is considered blank/not loaded


def blank_halves(png_path: Path) -> list[str]:
    labels = ("left", "right")
    scores = page_half_text_density(png_path)
    return [
        labels[i]
        for i, s in enumerate(scores)
        if s is not None and s < BLANK_SCORE
    ]


CLIP_RUN_FRAC = 0.08  # contiguous bright edge run longer than this => clipped


def clipped_edges(png_path: Path) -> list[str]:
    """Image edges that cut into the book page (paper touching the border).

    A correct capture of the reader has dark background all around the book.
    A long CONTIGUOUS run of bright (paper) pixels along an outer row/column
    means part of the page is outside the frame — e.g. when a site banner
    pushed the reader down and the bottom of the spread was cut off (the
    page_182 case). Short bright specks (toolbar icons) are ignored.
    """
    from PIL import Image

    with Image.open(png_path) as im:
        gray = im.convert("L")
    w, h = gray.size
    px = gray.load()

    def max_bright_run(coords) -> int:
        run = best = 0
        for x, y in coords:
            if px[x, y] >= 185:
                run += 1
                if run > best:
                    best = run
            else:
                run = 0
        return best

    edges: list[str] = []
    if max_bright_run((x, 1) for x in range(w)) > w * CLIP_RUN_FRAC:
        edges.append("top")
    if max_bright_run((x, h - 2) for x in range(w)) > w * CLIP_RUN_FRAC:
        edges.append("bottom")
    if max_bright_run((1, y) for y in range(h)) > h * CLIP_RUN_FRAC:
        edges.append("left")
    if max_bright_run((w - 2, y) for y in range(h)) > h * CLIP_RUN_FRAC:
        edges.append("right")
    return edges


def spread_problems(png_path: Path) -> list[str]:
    problems = [f"{half} half blank" for half in blank_halves(png_path)]
    problems += [f"clipped at {edge}" for edge in clipped_edges(png_path)]
    return problems


def dismiss_overlays(page) -> None:
    """Best-effort removal of Archive.org banners that shift the reader.

    Donation/announcement banners can push the BookReader partly out of the
    viewport, producing clipped captures.
    """
    try:
        page.evaluate(
            """
            () => {
              const sels = [
                '#donato', '#donato-banner', '.donation-banner',
                'ia-banners', '#ia-banners', '#npe-banner', '.banner-mobile',
              ];
              for (const sel of sels) {
                document.querySelectorAll(sel).forEach(el => el.remove());
              }
              window.dispatchEvent(new Event('resize'));
            }
            """
        )
    except Exception:
        pass


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


def capture_spread(page, dest: Path, wait_budget_s: float) -> list[str]:
    """Screenshot the reader; re-shoot while the capture looks defective.

    Detects two failure modes: a page half that is still blank (not loaded
    yet) and a page clipped by the frame edge (layout pushed by a banner).
    Returns the problems that remain after the budget is spent (a genuinely
    blank page also stays on this list).
    """
    deadline = time.monotonic() + wait_budget_s
    screenshot_reader(page, dest)
    problems = spread_problems(dest)
    while problems and time.monotonic() < deadline:
        if any(p.startswith("clipped") for p in problems):
            dismiss_overlays(page)
        page.wait_for_timeout(2500)
        screenshot_reader(page, dest)
        problems = spread_problems(dest)
    return problems


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
                loc.scroll_into_view_if_needed(timeout=3000)
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
    p.add_argument(
        "--slugs",
        nargs="+",
        default=None,
        help="Re-capture only these slugs (e.g. page_086), implies --overwrite",
    )
    p.add_argument(
        "--blank-wait",
        type=float,
        default=20.0,
        help="First-pass wait budget (s) for a blank-looking page half",
    )
    p.add_argument(
        "--retry-wait",
        type=float,
        default=120.0,
        help="Second-pass wait budget (s) for spreads that still had blank halves",
    )
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
            suspects: list[tuple[str, str, list[str]]] = []  # (url, slug, blank halves)

            for idx, url in enumerate(urls, start=1):
                slug = slug_from_url(url)
                if args.slugs is not None:
                    if slug not in args.slugs:
                        continue
                elif not started:
                    if slug == args.start_from:
                        started = True
                    else:
                        skipped += 1
                        continue

                dest = args.out_dir / f"{slug}.png"
                if dest.exists() and not args.overwrite and args.slugs is None:
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

                dismiss_overlays(page)
                try:
                    wait_for_book_images(page)
                except PlaywrightTimeout:
                    print("  WARN: book images not detected in time; capturing anyway", flush=True)

                problems = capture_spread(page, dest, args.blank_wait)
                size_kb = dest.stat().st_size / 1024
                if problems:
                    print(
                        f"  saved {dest.name} ({size_kb:.0f} KiB); "
                        f"{'; '.join(problems)} -> second pass",
                        flush=True,
                    )
                    suspects.append((url, slug, problems))
                else:
                    print(f"  saved {dest.name} ({size_kb:.0f} KiB)", flush=True)
                done += 1
                time.sleep(args.delay)

                if args.limit is not None and done >= args.limit:
                    print(f"Limit {args.limit} reached.")
                    break

            # Second pass: revisit defective spreads and wait much longer.
            # A half that stays blank is most likely a genuinely blank page,
            # but everything left here is listed for manual review.
            still_bad: list[tuple[str, list[str]]] = []
            if suspects:
                print(
                    f"\nSecond pass: {len(suspects)} defective spread(s), "
                    f"waiting up to {args.retry_wait:.0f}s each...",
                    flush=True,
                )
                for url, slug, prev_problems in suspects:
                    dest = args.out_dir / f"{slug}.png"
                    print(f"  retry {slug} ({'; '.join(prev_problems)})", flush=True)
                    try:
                        goto_with_retries(page, url)
                        page.wait_for_timeout(500)
                        dismiss_overlays(page)
                        try:
                            wait_for_book_images(page, timeout_ms=int(args.retry_wait * 1000))
                        except PlaywrightTimeout:
                            pass
                        problems = capture_spread(page, dest, args.retry_wait)
                    except Exception as exc:
                        print(f"    FAILED: {exc}", flush=True)
                        still_bad.append((slug, prev_problems))
                        continue
                    if problems:
                        print(f"    still defective: {'; '.join(problems)}", flush=True)
                        still_bad.append((slug, problems))
                    else:
                        print("    recovered: capture looks complete now", flush=True)

            print(f"\nFinished. Captured/kept={done}, skipped_before_start={skipped}, out={args.out_dir}")
            if still_bad:
                print(
                    "Spreads still defective after second pass (verify manually; "
                    "blank halves may be genuinely blank pages):"
                )
                for slug, problems in still_bad:
                    print(f"  {slug}: {', '.join(problems)}")
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
