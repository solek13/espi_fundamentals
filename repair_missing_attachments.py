#!/usr/bin/env python3
"""Redownload missing ESPI attachment files from saved metadata."""

import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import requests

from scrape_espi_periodic import (
    DOWNLOAD_DELAY,
    OUTPUT_DIR,
    STATE_DIR,
    SiteBlockedError,
    is_block_page,
    make_session,
)

LOG_FILE = os.path.join(STATE_DIR, "repair_missing_attachments.log")
CHUNK_SIZE = 65536


def setup_logging() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


def iter_missing(root: Path, pdf_only: bool):
    for metadata_path in root.rglob("metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.warning("Skipping bad metadata %s: %s", metadata_path, exc)
            continue

        attachments_dir = metadata_path.parent / "attachments"
        for attachment in metadata.get("attachments") or []:
            filename = attachment.get("filename") or ""
            url = attachment.get("url") or ""
            if not filename or not url:
                continue
            if pdf_only and not filename.lower().endswith(".pdf"):
                continue

            target = attachments_dir / filename
            if not target.exists():
                yield url, target


def download_one(session: requests.Session, url: str, target: Path) -> bool:
    time.sleep(random.uniform(*DOWNLOAD_DELAY))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(target.name + ".part")

    with session.get(url, timeout=60, stream=True) as resp:
        if resp.status_code != 200:
            logging.warning("HTTP %d for %s", resp.status_code, url)
            return False

        first_chunk = next(resp.iter_content(chunk_size=CHUNK_SIZE), b"")
        content_type = resp.headers.get("Content-Type", "").lower()
        if (
            "text/html" in content_type
            and is_block_page(first_chunk.decode("utf-8", errors="ignore"))
        ):
            raise SiteBlockedError(
                f"PAP returned an Incapsula block page for {url}"
            )

        with tmp_path.open("wb") as handle:
            if first_chunk:
                handle.write(first_chunk)
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    handle.write(chunk)

    if tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        logging.warning("Empty download for %s", url)
        return False

    tmp_path.replace(target)
    logging.info("Downloaded %s (%.1f KB)", target, target.stat().st_size / 1024)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Redownload missing ESPI attachments listed in metadata"
    )
    parser.add_argument("--root", type=Path, default=Path(OUTPUT_DIR))
    parser.add_argument(
        "--all-attachments",
        action="store_true",
        help="Repair every missing attachment type, not only PDFs",
    )
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    setup_logging()
    session = make_session()
    pdf_only = not args.all_attachments
    missing = list(iter_missing(args.root, pdf_only=pdf_only))
    if args.limit:
        missing = missing[: args.limit]

    logging.info(
        "Missing %s attachments to repair: %d",
        "PDF" if pdf_only else "all",
        len(missing),
    )

    done = 0
    failed = 0
    try:
        for index, (url, target) in enumerate(missing, start=1):
            logging.info("Repair %d/%d: %s", index, len(missing), target.name)
            try:
                if download_one(session, url, target):
                    done += 1
                else:
                    failed += 1
            except requests.RequestException as exc:
                failed += 1
                logging.warning("Request failed for %s: %s", url, exc)
    except SiteBlockedError as exc:
        logging.error("STOPPED: %s", exc)
        return 2

    logging.info("Repair finished. Downloaded: %d | Failed: %d", done, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
