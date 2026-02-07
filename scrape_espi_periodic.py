#!/usr/bin/env python3
"""
ESPI Periodic Reports Scraper
Downloads all periodic (okresowe) reports from https://biznes.pap.pl/espi/periodic/
Organizes them into company/report-type folder hierarchy.
Resumable via JSON state tracking. Rate-limited to be polite.
"""

import json
import logging
import os
import re
import sys
import time
import random
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────

BASE_URL = "https://biznes.pap.pl"
OUTPUT_DIR = "/media/solek/16TB1/espi_periodic"
STATE_DIR = os.path.join(OUTPUT_DIR, "_state")
LOG_FILE = os.path.join(OUTPUT_DIR, "_state", "scraper.log")

YEARS = range(2026, 2012, -1)  # 2026 down to 2013
PAGE_DELAY = (2.0, 4.0)        # seconds between report page fetches
DOWNLOAD_DELAY = (0.5, 1.5)    # seconds between attachment downloads
LISTING_DELAY = (1.0, 2.0)     # seconds between day listing fetches
BULK_PAUSE_EVERY = 50           # extra pause every N reports
BULK_PAUSE_SECS = 10

MAX_RETRIES = 3
BACKOFF_BASE = 30  # seconds for first retry on 429/5xx

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

# ── Report type classification ─────────────────────────────────────────────

REPORT_TYPE_MAP = {
    "SRR":    "roczny_skonsolidowany",
    "RR":     "roczny",
    "QSr":    "kwartalny_skonsolidowany",
    "SA-QSr": "kwartalny_skonsolidowany",
    "QS":     "kwartalny_skonsolidowany",
    "SA-Q":   "kwartalny",
    "Q":      "kwartalny",
    "PSr":    "polroczny_skonsolidowany",
    "SA-PSr": "polroczny_skonsolidowany",
    "SA-P":   "polroczny",
    "SA-PS":  "polroczny",
    "P":      "polroczny",
    "PS":     "polroczny",
}

# ── Logging setup ──────────────────────────────────────────────────────────

def setup_logging():
    os.makedirs(STATE_DIR, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


# ── State management ──────────────────────────────────────────────────────

def load_downloaded() -> set:
    path = os.path.join(STATE_DIR, "downloaded.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_downloaded(downloaded: set):
    path = os.path.join(STATE_DIR, "downloaded.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(downloaded), f, indent=0)


def load_progress() -> dict:
    path = os.path.join(STATE_DIR, "progress.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_progress(year: int, month: int, day: int):
    path = os.path.join(STATE_DIR, "progress.json")
    data = {"year": year, "month": month, "day": day,
            "updated": datetime.now().isoformat()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── HTTP helpers ──────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl,en;q=0.5",
    })
    return s


def fetch_with_retry(session: requests.Session, url: str,
                     delay_range: tuple = PAGE_DELAY,
                     stream: bool = False) -> requests.Response | None:
    """Fetch URL with retry + backoff on transient errors."""
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(random.uniform(*delay_range))
            resp = session.get(url, timeout=60, stream=stream)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 5)
                logging.warning(
                    "HTTP %d for %s – retrying in %.0fs (attempt %d/%d)",
                    resp.status_code, url, wait, attempt + 1, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            logging.error("HTTP %d for %s – skipping", resp.status_code, url)
            return None
        except requests.RequestException as e:
            wait = BACKOFF_BASE * (2 ** attempt)
            logging.warning(
                "Request error for %s: %s – retrying in %.0fs (attempt %d/%d)",
                url, e, wait, attempt + 1, MAX_RETRIES,
            )
            time.sleep(wait)
    logging.error("All retries exhausted for %s", url)
    return None


# ── Name sanitization ─────────────────────────────────────────────────────

def sanitize_name(name: str) -> str:
    """Create a safe filesystem name from a company name or slug."""
    # Normalize unicode
    name = unicodedata.normalize("NFKD", name)
    # Replace common Polish chars that NFKD decomposes
    name = name.encode("ascii", "ignore").decode("ascii")
    # Replace problematic filesystem chars
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Collapse whitespace and dots
    name = re.sub(r'[\s.]+', '_', name)
    # Remove leading/trailing underscores
    name = name.strip('_')
    # Limit length
    if len(name) > 120:
        name = name[:120].rstrip('_')
    return name or "unknown"


# ── Report type classification ─────────────────────────────────────────────

def classify_report(type_code: str, title: str) -> str:
    """Classify report into subfolder based on type code and title."""
    code = type_code.strip()

    # Direct match on known codes
    if code in REPORT_TYPE_MAP:
        return REPORT_TYPE_MAP[code]

    # Title-based fallback (case-insensitive)
    t = title.lower()
    is_consolidated = "skonsolidowan" in t
    is_annual = "roczn" in t
    is_semi = "półroczn" in t or "polroczn" in t or "póroczy" in t
    is_quarterly = "kwartal" in t

    if is_annual and is_consolidated:
        return "roczny_skonsolidowany"
    if is_annual:
        return "roczny"
    if is_semi and is_consolidated:
        return "polroczny_skonsolidowany"
    if is_semi:
        return "polroczny"
    if is_quarterly and is_consolidated:
        return "kwartalny_skonsolidowany"
    if is_quarterly:
        return "kwartalny"

    return "inne"


# ── Calendar parsing ──────────────────────────────────────────────────────

def get_year_calendar(session: requests.Session, year: int) -> list[tuple[int, int, int]]:
    """Parse the year calendar page, return list of (month, day, count) with reports."""
    url = f"{BASE_URL}/espi/periodic/{year}?company&selectCompany"
    resp = fetch_with_retry(session, url, delay_range=(1.0, 2.0))
    if not resp:
        logging.error("Failed to fetch calendar for year %d", year)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # The calendar is the second <table class="table"> on the page.
    # It's a 12x31 grid: first cell per row = month number, then days 1-31.
    # Identify it by finding the table whose first data row starts with "1" (month).
    tables = soup.find_all("table", class_="table")
    table = None
    for t in tables:
        rows = t.find_all("tr")
        if len(rows) >= 13:  # header + 12 months
            # Check if second row starts with month "1"
            first_data = rows[1].find_all("td")
            if first_data and first_data[0].get_text(strip=True) == "1":
                table = t
                break
    if not table:
        logging.warning("No calendar table found for year %d", year)
        return []

    days_with_reports = []
    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if not cells:
            continue
        # First cell is the month number
        month_text = cells[0].get_text(strip=True)
        if not month_text.isdigit():
            continue
        month = int(month_text)

        # Remaining cells are days 1-31
        for day_idx, cell in enumerate(cells[1:], start=1):
            link = cell.find("a")
            if link:
                count_text = link.get_text(strip=True)
                try:
                    count = int(count_text)
                except ValueError:
                    count = 1
                days_with_reports.append((month, day_idx, count))

    logging.info("Year %d: found %d days with reports (%d total reports)",
                 year, len(days_with_reports),
                 sum(c for _, _, c in days_with_reports))
    return days_with_reports


# ── Day report listing ────────────────────────────────────────────────────

def get_day_reports(session: requests.Session, year: int, month: int, day: int) -> list[dict]:
    """Fetch AJAX listing for a day, return list of report dicts."""
    url = (f"{BASE_URL}/articles/periodic/{year}/{month}/{day}"
           f"?limit=100&page=0&company=&selectCompany=")
    resp = fetch_with_retry(session, url, delay_range=LISTING_DELAY)
    if not resp:
        return []

    # Response may be wrapped in <textarea> tags — strip them
    text = resp.text.strip()
    if text.startswith("<textarea>"):
        text = text[len("<textarea>"):]
    if text.endswith("</textarea>"):
        text = text[:-len("</textarea>")]

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logging.error("Invalid JSON for day listing %d-%02d-%02d", year, month, day)
        return []

    # Extract HTML from Drupal AJAX response
    html = ""
    for cmd in data:
        if isinstance(cmd, dict) and cmd.get("command") == "insert":
            html = cmd.get("data", "")
            break

    if not html:
        logging.warning("No HTML in AJAX response for %d-%02d-%02d", year, month, day)
        return []

    soup = BeautifulSoup(html, "html.parser")
    reports = []

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        # Skip the day header row
        if tds[0].get("colspan"):
            continue

        time_text = tds[0].get_text(strip=True)
        type_code = tds[1].get_text(strip=True)
        company_tag = tds[2].find("a")
        title_tag = tds[3].find("a")

        if not title_tag or not title_tag.get("href"):
            continue

        company_name = tds[2].get_text(strip=True)
        title = tds[3].get_text(strip=True)
        report_url = title_tag["href"]

        # Normalize URL
        if report_url.startswith("/"):
            report_url = BASE_URL + report_url

        reports.append({
            "time": time_text,
            "type_code": type_code,
            "company": company_name,
            "title": title,
            "url": report_url,
            "date": f"{year}-{month:02d}-{day:02d}",
        })

    return reports


# ── Individual report download ────────────────────────────────────────────

def download_report(session: requests.Session, report: dict, downloaded: set) -> bool:
    """Download a single report page + attachments. Returns True if successful."""
    url = report["url"]
    slug = url.rstrip("/").split("/")[-1]

    # Skip if already downloaded
    if slug in downloaded:
        return False

    logging.info("Downloading: %s – %s", report["company"], report["title"][:80])

    # Fetch report page
    resp = fetch_with_retry(session, url)
    if not resp:
        logging.error("Failed to fetch report page: %s", url)
        return False

    page_html = resp.text
    soup = BeautifulSoup(page_html, "html.parser")

    # Extract publication date from page (more reliable than listing)
    pub_date = report["date"]
    date_div = soup.find("div", class_="publicationDate")
    if date_div:
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', date_div.get_text())
        if date_match:
            pub_date = date_match.group(1)

    # Classify report type
    report_type = classify_report(report["type_code"], report["title"])

    # Build output path
    company_dir = sanitize_name(report["company"])
    report_dir_name = f"{pub_date}_{sanitize_name(slug)}"
    report_path = os.path.join(
        OUTPUT_DIR, company_dir, report_type, report_dir_name
    )
    attachments_path = os.path.join(report_path, "attachments")
    os.makedirs(attachments_path, exist_ok=True)

    # Save report HTML
    htm_path = os.path.join(report_path, "report.htm")
    with open(htm_path, "w", encoding="utf-8") as f:
        f.write(page_html)

    # Find and download attachments
    attachment_links = soup.find_all("a", href=re.compile(r"/download/attachment/"))
    attachment_info = []
    for link in attachment_links:
        href = link["href"]
        if href.startswith("/"):
            href = BASE_URL + href
        filename = href.split("/")[-1]
        # Clean filename
        filename = re.sub(r'[<>:"|?*]', '_', filename)
        if not filename:
            filename = "attachment"

        att_path = os.path.join(attachments_path, filename)
        if not os.path.exists(att_path):
            att_resp = fetch_with_retry(session, href,
                                        delay_range=DOWNLOAD_DELAY, stream=True)
            if att_resp:
                try:
                    with open(att_path, "wb") as f:
                        for chunk in att_resp.iter_content(chunk_size=65536):
                            f.write(chunk)
                    size = os.path.getsize(att_path)
                    logging.info("  Attachment: %s (%.1f KB)", filename, size / 1024)
                except Exception as e:
                    logging.error("  Failed to save attachment %s: %s", filename, e)
                    if os.path.exists(att_path):
                        os.remove(att_path)
            else:
                logging.warning("  Failed to download attachment: %s", href)

        attachment_info.append({
            "filename": filename,
            "url": href,
            "display_text": link.get_text(strip=True),
        })

    # Save metadata
    metadata = {
        "company": report["company"],
        "title": report["title"],
        "type_code": report["type_code"],
        "report_type": report_type,
        "publication_date": pub_date,
        "listing_date": report["date"],
        "listing_time": report["time"],
        "url": report["url"],
        "slug": slug,
        "attachments": attachment_info,
        "downloaded_at": datetime.now().isoformat(),
    }
    meta_path = os.path.join(report_path, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    # Mark as downloaded
    downloaded.add(slug)
    return True


# ── Main orchestrator ─────────────────────────────────────────────────────

def main():
    setup_logging()
    logging.info("=" * 60)
    logging.info("ESPI Periodic Reports Scraper started")
    logging.info("Output directory: %s", OUTPUT_DIR)
    logging.info("=" * 60)

    os.makedirs(STATE_DIR, exist_ok=True)
    downloaded = load_downloaded()
    logging.info("Previously downloaded: %d reports", len(downloaded))

    session = make_session()
    total_new = 0
    save_counter = 0

    for year in YEARS:
        logging.info("── Processing year %d ──", year)
        days = get_year_calendar(session, year)
        if not days:
            logging.info("No report days found for %d, skipping", year)
            continue

        # Sort by month, then day
        days.sort(key=lambda x: (x[0], x[1]))

        for month, day, expected_count in days:
            logging.info("  Day %d-%02d-%02d (expected ~%d reports)",
                         year, month, day, expected_count)

            reports = get_day_reports(session, year, month, day)
            if not reports:
                logging.info("  No reports parsed for %d-%02d-%02d", year, month, day)
                continue

            logging.info("  Found %d reports for %d-%02d-%02d",
                         len(reports), year, month, day)

            for report in reports:
                slug = report["url"].rstrip("/").split("/")[-1]
                if slug in downloaded:
                    continue

                success = download_report(session, report, downloaded)
                if success:
                    total_new += 1
                    save_counter += 1

                    # Periodic state save
                    if save_counter >= 5:
                        save_downloaded(downloaded)
                        save_counter = 0

                    # Bulk pause
                    if total_new % BULK_PAUSE_EVERY == 0:
                        logging.info(
                            "  Bulk pause after %d new reports (%.0fs)...",
                            total_new, BULK_PAUSE_SECS,
                        )
                        time.sleep(BULK_PAUSE_SECS)

            save_progress(year, month, day)

        # Save state after each year
        save_downloaded(downloaded)
        logging.info("Year %d complete. Total new downloads so far: %d", year, total_new)

    save_downloaded(downloaded)
    logging.info("=" * 60)
    logging.info("Scraper finished. Total new downloads: %d", total_new)
    logging.info("Total in manifest: %d", len(downloaded))
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
