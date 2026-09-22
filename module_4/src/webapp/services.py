"""
services.py - The work behind the two buttons, kept out of routes.py on purpose.

Module 3's Pull Data ran as a detached subprocess coordinated through a file
lock and a status file on disk, because a full scrape could take ~90 minutes
and the whole point was to survive a server restart.  That design is very
hard to unit test: a real test would have to start a real subprocess, poll a
real file, and wait — exactly the kind of slow, flaky, sleep()-based test the
assignment forbids.

Module 4's "Pull Data" is scoped narrower (only entries newer than what is
already stored, via GradCafeScraper.scrape_new_entries), which finishes in
seconds to at most a couple of minutes.  That lets the whole pipeline run
in-process, inside the request, which makes it trivially testable: a test
just swaps SCRAPE_FN and LOAD_FN for fakes and calls the route directly.

PullState.is_running is the "busy" flag both buttons check.  It is a plain
boolean guarded by a lock -- not a sleep(), not a poll loop -- so tests can
set it directly to force the busy path (see tests/test_buttons.py).
"""

from __future__ import annotations

import threading


class PullState:
    """Tracks whether a pull is currently in progress, for one Flask process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def try_start(self) -> bool:
        """Atomically flip idle -> running. Returns False if a pull is already running."""
        with self._lock:
            if self._running:
                return False
            self._running = True
            return True

    def finish(self) -> None:
        """Always called from a finally: block, so a crash never leaves the app stuck busy."""
        with self._lock:
            self._running = False


def default_scrape_fn() -> list[dict]:
    """Production scraper: entries newer than what PostgreSQL already has.

    Imports are local so importing this module never requires network access
    or a live database (tests never call this function; they inject fakes).
    """
    from load_data import connect
    from scrape import GradCafeScraper

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT p_id FROM applicants")
        known_ids = {row[0] for row in cur}

    scraper = GradCafeScraper(cache_html=False)
    entries, _pages, _resume_url = scraper.scrape_new_entries(known_ids, max_pages=50)
    return entries


def default_load_fn(raw_entries: list[dict]) -> int:
    """Production loader: clean the raw entries, then insert them.

    Standardizing program/university names with the local LLM is a separate,
    optional, much slower step (see module_4/llm_hosting); new rows land with
    llm_generated_program/llm_generated_university left NULL until that step
    is run, the same way any other not-yet-standardized row would.
    """
    from clean import clean_data
    from load_data import connect, create_table, load_records

    if not raw_entries:
        return 0
    cleaned = clean_data(raw_entries)
    with connect() as conn:
        create_table(conn)
        inserted, _present, _unusable = load_records(conn, cleaned)
    return inserted
