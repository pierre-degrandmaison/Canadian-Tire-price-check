# Price Monitor

Daily monitors product prices from a list of URLs and produces a self-contained
**HTML status page** (`status.html`) you can open in any browser.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium   # one-time Chromium download (~150 MB)
```

## Configuration

Set the following environment variables before running:

| Variable       | Required | Description                               | Default              |
|----------------|----------|-------------------------------------------|----------------------|
| `CSV_FILE`     | No       | Path to the watched URLs CSV file         | `urls.csv`           |
| `REPORT_FILE`  | No       | Path for the generated HTML report        | `status.html`        |
| `CHECK_HOUR`   | No       | Hour of day to run the check (UTC, 0–23)  | `8`                  |
| `CHECK_MINUTE` | No       | Minute of the hour to run (0–59)          | `0`                  |
| `HISTORY_FILE` | No       | Path for the persistent change log JSON   | `status_history.json`|

Example:

```bash
export CHECK_HOUR=9          # run at 09:30 UTC every day
export CHECK_MINUTE=30
export REPORT_FILE=/var/www/html/prices.html   # serve from a web root
```

## CSV format

`urls.csv` — no header row, two columns:

```
"<product URL>","<current price>"
```

Example:

```
"https://www.canadiantire.ca/en/pdp/pwr-pod-20v-multi-pack-dual-port-charger-0547567p.html","$79.99"
```

The file is updated **in place** whenever a price change is detected, so the new
price becomes the baseline for the next check.

## Usage

**Run a single check now:**

```bash
python monitor.py
```

Open `status.html` in your browser once the check finishes.

**Run on a daily schedule (blocks the terminal):**

```bash
python monitor.py --schedule
```

With `--schedule`, the monitor runs one immediate check at startup and repeats
every day at `CHECK_HOUR:00 UTC`. The HTML report is regenerated after each run.

## Output files

| File                   | Description                                           |
|------------------------|-------------------------------------------------------|
| `status.html`          | Self-contained HTML report — open in any browser      |
| `urls.csv`             | Updated in-place with the latest prices               |
| `status_history.json`  | Persisted change-event log (feeds the history table)  |

## What the HTML report shows

- **Summary banner** – green ("All prices unchanged") or amber ("N changes detected")
- **Watched Items table** – current price, previous price (struck-through), OK/CHANGED badge
- **Change History table** – every price change ever recorded, most recent first, with a ▲/▼ direction indicator

## How scraping works

1. **Canadian Tire URLs** → Playwright headless Chromium. The site uses React +
   Akamai bot-protection, so plain HTTP clients are blocked. Playwright runs a
   real browser that executes JS and bypasses the check.
2. **All other URLs** → lightweight `requests` + BeautifulSoup scan using common
   price selectors. Automatically falls back to Playwright if BS4 finds nothing.
