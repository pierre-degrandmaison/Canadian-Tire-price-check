"""
Price Monitor – runs daily, checks every URL in urls.csv, and produces a
self-contained HTML status page you can open in any browser.

Usage
-----
    python monitor.py              # run one check cycle now
    python monitor.py --schedule   # run on a daily schedule (blocks)

Configuration (environment variables)
--------------------------------------
    CSV_FILE            Path to the watched URLs CSV file      (default: urls.csv)
    REPORT_FILE         Path for the generated HTML report     (default: status.html)
    CHECK_HOUR          Hour of day to run (24 h, UTC)         (default: 8)
    CHECK_MINUTE        Minute of the hour to run (0–59)       (default: 0)
    PLAYWRIGHT_TIMEOUT  Browser page-load timeout in ms        (default: 30000)

Scraping strategy (updated)
-----------------------------
  Canadian Tire  → headed Chromium (headless=False) + playwright-stealth.
                   Intercepts the productFamily JSON API response to read the
                   price — no CSS selector scraping needed.
                   Akamai bot detection suppresses the price XHR in headless
                   mode, so headed mode is required.
  All other URLs → requests + BeautifulSoup; Playwright fallback for JS pages.

CSV format (no header row required)
-------------------------------------
    "<url>","<current_price>"

    e.g.  "https://www.canadiantire.ca/en/pdp/...", "$79.99"

The CSV is updated in-place whenever a price change is detected so the new
price becomes the baseline for the next check.

Dependencies
-------------
    pip install -r requirements.txt
    playwright install chromium   # one-time browser download

Scraping strategy
------------------
  1. Canadian Tire URLs  → Playwright headless Chromium (handles JS + bot checks).
  2. All other URLs      → requests + BeautifulSoup, with Playwright as fallback.
"""

import csv
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CSV_FILE          = Path(os.environ.get("CSV_FILE",    "urls.csv"))
REPORT_FILE       = Path(os.environ.get("REPORT_FILE", "status.html"))
CHECK_HOUR        = int(os.environ.get("CHECK_HOUR",        "8"))
CHECK_MINUTE      = int(os.environ.get("CHECK_MINUTE",      "0"))
PLAYWRIGHT_TIMEOUT = int(os.environ.get("PLAYWRIGHT_TIMEOUT", "30000"))  # ms

# Realistic browser headers for requests-based fetches
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ---------------------------------------------------------------------------
# Playwright helpers
# ---------------------------------------------------------------------------

def _playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _stealth_available() -> bool:
    try:
        from playwright_stealth import Stealth  # noqa: F401
        return True
    except ImportError:
        return False


def _fetch_price_with_playwright(url: str, css_selectors: list[str]) -> Optional[str]:
    """
    Open *url* in a headed Chromium browser (headless=False avoids bot detection
    on most sites), wait for the page to settle, then try each CSS selector to
    extract the price text.  Returns the first matching price string, or None.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.warning("Playwright not installed – cannot scrape JS-heavy pages.")
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=False,   # headed mode: bot-detection cannot see headless flags
                args=["--no-sandbox"],
            )
            ctx = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                locale="en-CA",
                extra_http_headers={"Accept-Language": "en-CA,en;q=0.9"},
            )
            if _stealth_available():
                from playwright_stealth import Stealth
                Stealth().use_sync(ctx)
            page = ctx.new_page()
            page.goto(url, wait_until="commit", timeout=PLAYWRIGHT_TIMEOUT)
            page.wait_for_timeout(4_000)

            for selector in css_selectors:
                try:
                    el = page.query_selector(selector)
                    if el:
                        text = el.get_attribute("content") or el.inner_text()
                        text = text.strip()
                        if text and _PRICE_TEXT_RE.search(text):
                            log.debug("Playwright matched %r → %r", selector, text)
                            browser.close()
                            return text
                except PWTimeout:
                    continue

            browser.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("Playwright fetch failed for %s: %s", url, exc)
    return None


# ---------------------------------------------------------------------------
# Canadian Tire specific scraping
# ---------------------------------------------------------------------------
#
# How it works
# ------------
# The Canadian Tire product page is a React SPA protected by Akamai Bot Manager.
# In headless mode Chromium is detected and the price XHR is silently suppressed.
# In headed mode (headless=False) the page runs normally and fires a call to:
#
#   /api/v1/product/api/v2/product/productFamily/<sku>?baseStoreId=CTR&…
#
# We intercept that response, parse the JSON, and extract currentPrice.value.
# No CSS selector or DOM scraping is needed.

def _fetch_canadiantire_price(url: str) -> Optional[str]:
    """
    Launch a headed Chromium browser, navigate to the CT product page, intercept
    the productFamily API response, and return the currentPrice formatted as
    '$XX.XX'.  Returns None if Playwright is not installed or the price cannot
    be found.
    """
    if not _playwright_available():
        log.warning(
            "Playwright is required for Canadian Tire pages. "
            "Run: pip install playwright && playwright install chromium"
        )
        return None

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False, args=["--no-sandbox"])
            ctx = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                locale="en-CA",
                extra_http_headers={"Accept-Language": "en-CA,en;q=0.9"},
            )
            if _stealth_available():
                from playwright_stealth import Stealth
                Stealth().use_sync(ctx)

            captured: dict = {}

            def _on_response(resp) -> None:
                if "productFamily" in resp.url and "canadiantire" in resp.url:
                    try:
                        captured["data"] = resp.json()
                        captured["url"]  = resp.url
                    except Exception:
                        pass

            page = ctx.new_page()
            page.on("response", _on_response)
            page.goto(url, wait_until="commit", timeout=PLAYWRIGHT_TIMEOUT)

            # Wait up to 15 s for the productFamily API response to arrive
            deadline = 15_000
            step = 500
            elapsed = 0
            while "data" not in captured and elapsed < deadline:
                page.wait_for_timeout(step)
                elapsed += step

            browser.close()

        if "data" not in captured:
            log.warning("CT productFamily API did not fire for %s", url)
            return None

        # Navigate the JSON: currentPrice → value
        data = captured["data"]
        skus = data.get("skus") or []
        # Pick the first SKU's currentPrice; fall back to top-level currentPrice
        for sku in skus:
            price_obj = sku.get("currentPrice") or sku.get("prices", {}).get("currentPrice")
            if price_obj:
                value = price_obj.get("value")
                if value is not None:
                    return f"${float(value):.2f}"
        # Top-level fallback
        top = data.get("currentPrice") or {}
        value = top.get("value")
        if value is not None:
            return f"${float(value):.2f}"

        log.warning("CT productFamily JSON had no currentPrice for %s", url)
        log.debug("CT productFamily JSON: %s", json.dumps(data)[:500])
        return None

    except Exception as exc:  # noqa: BLE001
        log.warning("CT price fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Generic HTML price scraper (BeautifulSoup + Playwright fallback)
# ---------------------------------------------------------------------------

_GENERIC_SELECTORS = [
    "[itemprop='price']",
    "[class*='selling-price']",
    "[class*='sellingPrice']",
    "[class*='price-value']",
    "[data-testid*='price']",
    "[data-automation*='price']",
    "span.price",
    "div.price",
    ".product-price",
    ".selling-price",
    ".reg-price",
    "[class*='price']",
    "[class*='Price']",
]

_PRICE_TEXT_RE = re.compile(r"[\$\€\£\¥]?\s*[\d,]+\.?\d*")


def _normalise_price(price: str) -> str:
    """
    Return a canonical numeric string for comparison (currency-symbol-agnostic).
    Handles North-American ($1,299.00) and European (€1.299,99 / €19,99) formats.
    """
    # European with dot-thousands + comma-decimal  e.g. 1.299,99  (try FIRST)
    m = re.search(r"\d{1,3}(?:\.\d{3})+,\d+", price)
    if m:
        return m.group(0).replace(".", "").replace(",", ".")
    # North-American: optional comma-thousands + period-decimal  e.g. 1,299.00
    m = re.search(r"\d{1,3}(?:,\d{3})*\.\d+", price)
    if m:
        return m.group(0).replace(",", "")
    # European without thousands separator  e.g. 19,99
    m = re.search(r"\d+,\d{1,2}(?!\d)", price)
    if m:
        return m.group(0).replace(",", ".")
    # Plain integer
    m = re.search(r"\d+", price)
    if m:
        return m.group(0)
    return price.strip()


def _extract_price_from_html(html: "str | bytes") -> Optional[str]:
    """Try common CSS selectors to pull a price-like string from raw HTML.

    Accepts either a decoded string or raw bytes. Passing bytes lets
    BeautifulSoup detect the charset from the HTML <meta> tags, which avoids
    mojibake (e.g. Â£ instead of £) when the HTTP Content-Type header is
    missing or incorrect.
    """
    soup = BeautifulSoup(html, "lxml")
    for selector in _GENERIC_SELECTORS:
        for tag in soup.select(selector):
            content = tag.get("content") or tag.get_text(strip=True)
            if content and _PRICE_TEXT_RE.search(content):
                return content.strip()
    return None


def _fetch_generic_price(url: str) -> Optional[str]:
    """requests + BS4 scrape, with Playwright fallback for JS-rendered pages."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        # Pass raw bytes so BeautifulSoup can detect the charset from <meta>
        # tags in the HTML itself, rather than relying on the HTTP Content-Type
        # header which is often absent or wrong (avoids Â£ / mojibake).
        price = _extract_price_from_html(resp.content)
        if price:
            return price
    except Exception as exc:  # noqa: BLE001
        log.warning("HTTP fetch failed for %s: %s", url, exc)

    if _playwright_available():
        log.info("Retrying %s with Playwright...", url[:60])
        return _fetch_price_with_playwright(url, _GENERIC_SELECTORS)
    return None


def fetch_price(url: str) -> Optional[str]:
    """Return the current price for *url*, or None if it cannot be determined."""
    if "canadiantire.ca" in url:
        price = _fetch_canadiantire_price(url)
    else:
        price = _fetch_generic_price(url)

    if price:
        log.info("Fetched  → %-60s  %s", url[:60], price)
    else:
        log.warning("Could not retrieve price for %s", url)
    return price


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv(path: Path) -> list[dict]:
    """Return a list of {"url": ..., "price": ...} dicts."""
    rows = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2:
                rows.append({"url": row[0].strip(), "price": row[1].strip()})
    return rows


def save_csv(path: Path, rows: list[dict]) -> None:
    """Write rows back to *path* atomically (temp-file + rename)."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        for row in rows:
            writer.writerow([row["url"], row["price"]])
    tmp.replace(path)


# ---------------------------------------------------------------------------
# History helpers  (status_history.json)
# ---------------------------------------------------------------------------

_HISTORY_FILE = Path(os.environ.get("HISTORY_FILE", "status_history.json"))


def _load_history() -> list[dict]:
    """Load the persisted change-event history (list of event dicts)."""
    if _HISTORY_FILE.exists():
        try:
            return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return []


def _save_history(history: list[dict]) -> None:
    """Persist the change-event history to disk."""
    _HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# HTML report generator
# ---------------------------------------------------------------------------

def _build_report(
    rows: list[dict],
    changes: list[dict],
    run_ts: str,
    history: list[dict],
) -> str:
    """Return a self-contained HTML status page as a string."""

    def _row_class(url: str) -> str:
        changed_urls = {c["url"] for c in changes}
        return "changed" if url in changed_urls else "ok"

    def _badge(url: str) -> str:
        changed_urls = {c["url"] for c in changes}
        if url in changed_urls:
            return '<span class="badge badge-changed">CHANGED</span>'
        return '<span class="badge badge-ok">OK</span>'

    def _short(url: str, n: int = 60) -> str:
        return url if len(url) <= n else url[:n] + "…"

    # Current prices table rows
    table_rows = ""
    for row in rows:
        url = row["url"]
        price = row["price"]
        rc = _row_class(url)
        old = next((c["old_price"] for c in changes if c["url"] == url), "")
        prev_cell = f'<span class="prev-price">{old}</span>' if old else "—"
        table_rows += f"""
        <tr class="{rc}">
          <td><a href="{url}" target="_blank" rel="noopener">{_short(url)}</a></td>
          <td class="price">{price}</td>
          <td class="price">{prev_cell}</td>
          <td>{_badge(url)}</td>
        </tr>"""

    # History table rows (most recent first)
    history_rows = ""
    for evt in reversed(history):
        direction = "▲" if _normalise_price(evt["new_price"]) > _normalise_price(evt["old_price"]) else "▼"
        dir_class = "up" if direction == "▲" else "down"
        history_rows += f"""
        <tr>
          <td class="ts">{evt["timestamp"]}</td>
          <td><a href="{evt['url']}" target="_blank" rel="noopener">{_short(evt['url'])}</a></td>
          <td class="price">{evt["old_price"]}</td>
          <td class="price">{evt["new_price"]}</td>
          <td class="direction {dir_class}">{direction}</td>
        </tr>"""

    if not history_rows:
        history_rows = '<tr><td colspan="5" class="empty">No changes recorded yet.</td></tr>'

    changed_count = len(changes)
    summary_class = "summary-changed" if changed_count else "summary-ok"
    summary_msg = (
        f"{changed_count} price change{'s' if changed_count != 1 else ''} detected"
        if changed_count
        else "All prices unchanged"
    )

    next_run = f"daily at {CHECK_HOUR:02d}:{CHECK_MINUTE:02d} UTC"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Price Monitor – Status</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    font-size: 14px;
    line-height: 1.6;
    background: #f7f8fa;
    color: #1f2328;
    padding: 32px 16px;
  }}
  .container {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; font-weight: 600; margin-bottom: 4px; }}
  .meta {{ color: #57606a; font-size: 0.85rem; margin-bottom: 24px; }}
  .summary {{
    display: inline-block;
    padding: 8px 16px;
    border-radius: 6px;
    font-weight: 600;
    margin-bottom: 28px;
    font-size: 0.95rem;
  }}
  .summary-ok      {{ background: #d1fae5; color: #065f46; border: 1px solid #6ee7b7; }}
  .summary-changed {{ background: #fef3c7; color: #92400e; border: 1px solid #fcd34d; }}
  h2 {{ font-size: 1rem; font-weight: 600; margin-bottom: 10px; color: #1f2328; }}
  section {{ margin-bottom: 36px; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    overflow: hidden;
  }}
  thead th {{
    background: #f0f2f5;
    text-align: left;
    padding: 10px 14px;
    font-weight: 600;
    font-size: 0.82rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #57606a;
    border-bottom: 1px solid #e5e7eb;
  }}
  tbody td {{
    padding: 10px 14px;
    border-bottom: 1px solid #f0f2f5;
    vertical-align: middle;
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr.changed {{ background: #fffbeb; }}
  tbody tr.ok:hover, tbody tr.changed:hover {{ background: #f7f8fa; }}
  .price {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
  .prev-price {{ color: #57606a; text-decoration: line-through; font-weight: 400; }}
  a {{ color: #3b82d4; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .badge {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.03em;
  }}
  .badge-ok      {{ background: #d1fae5; color: #065f46; }}
  .badge-changed {{ background: #fef3c7; color: #92400e; }}
  .direction {{ font-size: 1.1rem; font-weight: 700; text-align: center; }}
  .direction.up   {{ color: #dc2626; }}
  .direction.down {{ color: #16a34a; }}
  .ts {{ color: #57606a; white-space: nowrap; font-size: 0.82rem; }}
  .empty {{ color: #57606a; font-style: italic; text-align: center; padding: 20px; }}
  footer {{
    margin-top: 40px;
    padding-top: 16px;
    border-top: 1px solid #e5e7eb;
    text-align: center;
    font-size: 0.78rem;
    color: #57606a;
  }}
</style>
</head>
<body>
<div class="container">
  <h1>Price Monitor</h1>
  <p class="meta">Last checked: <strong>{run_ts}</strong> &nbsp;·&nbsp; Next run: {next_run}</p>
  <div class="summary {summary_class}">{summary_msg}</div>

  <section>
    <h2>Watched Items</h2>
    <table>
      <thead>
        <tr>
          <th>URL</th>
          <th>Current Price</th>
          <th>Previous Price</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>{table_rows}
      </tbody>
    </table>
  </section>

  <section>
    <h2>Change History</h2>
    <table>
      <thead>
        <tr>
          <th>Timestamp</th>
          <th>URL</th>
          <th>Old Price</th>
          <th>New Price</th>
          <th style="text-align:center">Dir</th>
        </tr>
      </thead>
      <tbody>{history_rows}
      </tbody>
    </table>
  </section>

  <footer>Price Monitor &nbsp;·&nbsp; Report generated {run_ts}</footer>
</div>
</body>
</html>"""


def write_report(
    rows: list[dict],
    changes: list[dict],
    run_ts: str,
    history: list[dict],
) -> None:
    """Write the HTML status report to REPORT_FILE."""
    html = _build_report(rows, changes, run_ts, history)
    tmp = REPORT_FILE.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(REPORT_FILE)
    log.info("Report written → %s", REPORT_FILE.resolve())


# ---------------------------------------------------------------------------
# Core check cycle
# ---------------------------------------------------------------------------

def run_check() -> None:
    """Load the CSV, check every URL, update prices, write the HTML report."""
    if not CSV_FILE.exists():
        log.error("CSV file not found: %s", CSV_FILE)
        return

    rows = load_csv(CSV_FILE)
    if not rows:
        log.warning("No rows found in %s", CSV_FILE)
        return

    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    changes: list[dict] = []

    for row in rows:
        url = row["url"]
        old_price = row["price"]
        new_price = fetch_price(url)

        if new_price is None:
            log.warning("Skipping update for %s (price unavailable)", url)
            continue

        if _normalise_price(new_price) != _normalise_price(old_price):
            log.info("PRICE CHANGED  %-60s  %s → %s", url[:60], old_price, new_price)
            changes.append({"url": url, "old_price": old_price, "new_price": new_price})
            row["price"] = new_price
        else:
            log.info("No change      %-60s  %s", url[:60], old_price)

    history = _load_history()
    if changes:
        save_csv(CSV_FILE, rows)
        for c in changes:
            history.append({
                "timestamp": run_ts,
                "url":       c["url"],
                "old_price": c["old_price"],
                "new_price": c["new_price"],
            })
        _save_history(history)
        log.info("%d price change(s) detected.", len(changes))
    else:
        log.info("No price changes detected.")

    write_report(rows, changes, run_ts, history)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if "--schedule" in sys.argv:
        try:
            from apscheduler.schedulers.blocking import BlockingScheduler
        except ImportError:
            log.error(
                "APScheduler is not installed. "
                "Run: pip install apscheduler  then retry."
            )
            sys.exit(1)

        scheduler = BlockingScheduler()
        scheduler.add_job(run_check, "cron", hour=CHECK_HOUR, minute=CHECK_MINUTE, timezone="UTC")
        log.info(
            "Scheduler started – will check prices daily at %02d:%02d UTC.",
            CHECK_HOUR, CHECK_MINUTE,
        )
        # Run once immediately so you don't wait 24 h for the first report
        run_check()
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            log.info("Scheduler stopped.")
    else:
        run_check()


if __name__ == "__main__":
    main()
