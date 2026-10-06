"""
incremental_scraper.py - Fetch only the Grad Café entries newer than the watermark.

The worker's "scrape_new_data" task (worker/consumer.py) does not re-read the
whole site.  Grad Café numbers its results in the order they are posted and
lists them newest first, so one number says where the stored data ends: the
watermark, ingestion_watermarks.last_seen, the highest result id already read.
fetch_new_entries(last_seen) hands GradCafeScraper.scrape_new_entries a
SeenUpTo(last_seen) as its "already stored" test, so the scraper

  * checks robots.txt first and refuses to run if the listing is disallowed;
  * reads the listing from the newest page, pausing between pages;
  * stops at the first page that shows an id at or below last_seen (everything
    after it is older), or after MAX_PAGES pages (about 20 entries each).

If MAX_PAGES runs out first, the entries it did fetch are still returned, and
NewEntries.resume_url says where the listing continues; the gap between
last_seen and the oldest entry fetched is logged as a warning, because the
next pull starts from the newest page again.

The scraper writes a copy of the robots.txt it obeyed into its data folder.
In a container the code lives in a folder the worker cannot write to, so that
folder comes from SCRAPE_DATA_DIR, or defaults to a gradcafe_scrape folder in
the system's temporary directory (/tmp in the worker image).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from worker.etl.scrape import GradCafeScraper

MAX_PAGES = 50   # the same cap as Module 3's Pull Data: at most 50 listing pages per pull
DATA_DIR_VARIABLE = "SCRAPE_DATA_DIR"

logger = logging.getLogger("gradcafe.incremental")


@dataclass(frozen=True)
class SeenUpTo:
    """The watermark as the scraper's "already stored" test: every result id up to
    *last_seen* is known.  None means nothing is known yet, so every entry is new."""

    last_seen: int | None

    def __contains__(self, result_id: object) -> bool:
        return (self.last_seen is not None and isinstance(result_id, int)
                and result_id <= self.last_seen)


@dataclass(frozen=True)
class NewEntries:
    """What one incremental pull fetched: the raw entries (newest first), the pages it
    read, and where the listing continues if MAX_PAGES ran out first (else None)."""

    entries: list[dict]
    pages: int
    resume_url: str | None

    @property
    def max_id(self) -> int | None:
        """The highest result id fetched, or None if nothing new was found."""
        return max((entry["result_id"] for entry in self.entries), default=None)

    @property
    def complete(self) -> bool:
        """True when the pull reached the watermark (or the end of the listing)."""
        return self.resume_url is None


def scrape_data_dir() -> Path:
    """The scraper's writable data folder: SCRAPE_DATA_DIR, or <temp dir>/gradcafe_scrape."""
    configured = os.environ.get(DATA_DIR_VARIABLE, "").strip()
    return Path(configured) if configured else Path(tempfile.gettempdir()) / "gradcafe_scrape"


def fetch_new_entries(last_seen: int | None, max_pages: int = MAX_PAGES) -> NewEntries:
    """Raw Grad Café entries with a result id above *last_seen* (all of them, up to
    *max_pages* pages, when it is None).  Network and robots.txt errors propagate."""
    scraper = GradCafeScraper(data_dir=scrape_data_dir(), cache_html=False)
    entries, pages, resume_url = scraper.scrape_new_entries(SeenUpTo(last_seen),
                                                            max_pages=max_pages)
    batch = NewEntries(entries, pages, resume_url)
    if not batch.complete:
        oldest = min((entry["result_id"] for entry in entries), default=None)
        logger.warning("page limit (%d) reached before the watermark (%s): entries older than "
                       "result id %s were not fetched", max_pages, last_seen, oldest)
    return batch
