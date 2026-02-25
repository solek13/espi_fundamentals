#!/usr/bin/env python3
"""
ESPI Daily Check – lightweight companion to scrape_espi_periodic.py
Checks only the last 7 days for new reports. Shares state with the full scraper.
Intended for daily cron execution.
"""

import json
import logging
import os
import sys
import time
import random
import fcntl
from datetime import datetime, timedelta

from scrape_espi_periodic import (
    BASE_URL,
    OUTPUT_DIR,
    STATE_DIR,
    USER_AGENT,
    REPORT_TYPE_MAP,
    SiteBlockedError,
    make_session,
    load_downloaded,
    save_downloaded,
    get_day_reports,
    download_report,
    fetch_with_retry,
)

# Lighter delays for daily check (few reports expected)
PAGE_DELAY = (5.0, 10.0)
LISTING_DELAY = (3.0, 6.0)
LOOKBACK_DAYS = 7

LOCK_FILE = os.path.join(STATE_DIR, "daily.lock")
DAILY_LOG = os.path.join(STATE_DIR, "daily.log")


def setup_daily_logging():
    os.makedirs(STATE_DIR, exist_ok=True)
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    fh = logging.FileHandler(DAILY_LOG, encoding="utf-8")
    fh.setFormatter(formatter)
    root.addHandler(sh)
    root.addHandler(fh)


def acquire_lock():
    """Prevent overlapping runs via file lock."""
    fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fp.write(str(os.getpid()))
        fp.flush()
        return fp
    except OSError:
        logging.error("Another instance is already running (lock: %s)", LOCK_FILE)
        sys.exit(0)


def main():
    setup_daily_logging()
    lock_fp = acquire_lock()

    logging.info("=" * 50)
    logging.info("ESPI daily check started")
    logging.info("=" * 50)

    downloaded = load_downloaded()
    logging.info("Manifest: %d reports", len(downloaded))

    session = make_session()
    total_new = 0
    today = datetime.now().date()

    try:
        for offset in range(LOOKBACK_DAYS):
            d = today - timedelta(days=offset)
            year, month, day = d.year, d.month, d.day

            reports = get_day_reports(session, year, month, day)
            new_on_day = sum(
                1 for r in reports
                if r["url"].rstrip("/").split("/")[-1] not in downloaded
            )
            if not reports:
                logging.info("  %s – no reports", d.isoformat())
                continue
            if new_on_day == 0:
                logging.info("  %s – %d reports, all known", d.isoformat(), len(reports))
                continue

            logging.info("  %s – %d reports, %d new", d.isoformat(), len(reports), new_on_day)

            for report in reports:
                slug = report["url"].rstrip("/").split("/")[-1]
                if slug in downloaded:
                    continue
                success = download_report(session, report, downloaded)
                if success:
                    total_new += 1

            save_downloaded(downloaded)

    except SiteBlockedError as e:
        logging.error("STOPPED: %s", e)

    save_downloaded(downloaded)
    logging.info("Daily check done. New downloads: %d | Total: %d", total_new, len(downloaded))
    logging.info("=" * 50)

    lock_fp.close()
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


if __name__ == "__main__":
    main()
