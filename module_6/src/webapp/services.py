"""
services.py - The work behind the two buttons, kept out of routes.py on purpose.

Module 3's Pull Data ran as a detached subprocess coordinated through a file
lock and a status file on disk, because a full scrape could take ~90 minutes
and the whole point was to survive a server restart.  That design is very
hard to unit test: a real test would have to start a real subprocess, poll a
real file, and wait — exactly the kind of slow, flaky, sleep()-based test the
assignment forbids.

The web app's "Pull Data" is scoped narrower (only entries newer than what is
already stored, via GradCafeScraper.scrape_new_entries), which finishes in
seconds to at most a couple of minutes.  That lets the whole pipeline run
in-process, inside the request, which makes it trivially testable: a test
just swaps SCRAPE_FN and LOAD_FN for fakes and calls the route directly.

PullState.is_running is the "busy" flag both buttons check.  It is a plain
boolean guarded by a lock -- not a sleep(), not a poll loop -- so tests can
set it directly to force the busy path (see tests/test_buttons.py).

default_search_fn serves GET /api/applicants: the one path where user input
reaches SQL (see applicant_search.py for the injection defences).
"""

from __future__ import annotations

import threading

from psycopg import sql

import applicant_search
import clean
import load_data
import scrape
from db_config import TABLE_NAME
from query_limits import MAX_LIMIT

# The stored ids that tell the scraper where new entries end.  The listing is
# newest-first and the scraper stops at the first page holding a stored id, so
# the newest MAX_LIMIT ids are all it needs -- never the whole table.
KNOWN_IDS_STATEMENT = sql.SQL("SELECT {key} FROM {table} ORDER BY {key} DESC LIMIT {limit}").format(
    key=sql.Identifier("p_id"), table=sql.Identifier(TABLE_NAME), limit=sql.Placeholder("limit")
)


class PullState:
    """Tracks whether a pull is currently in progress, for one Flask process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False

    @property
    def is_running(self) -> bool:
        """True while a pull is in progress."""
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

    Importing this module never requires network access or a live database;
    both are touched only when this function runs.  GradCafeScraper is looked
    up on the scrape module at call time, so a test can monkeypatch it.
    """
    with load_data.connect() as conn, conn.cursor() as cur:
        cur.execute(KNOWN_IDS_STATEMENT, {"limit": MAX_LIMIT})
        known_ids = {row[0] for row in cur}

    scraper = scrape.GradCafeScraper(cache_html=False)
    entries, _pages, _resume_url = scraper.scrape_new_entries(known_ids, max_pages=50)
    return entries


def default_load_fn(raw_entries: list[dict]) -> int:
    """Production loader: clean the raw entries, then insert them.

    Standardizing program/university names with the local LLM is a separate,
    optional, much slower step (see module_5/llm_hosting); new rows land with
    llm_generated_program/llm_generated_university left NULL until that step
    is run, the same way any other not-yet-standardized row would.

    It never creates or alters tables: that is an administrator's job
    (``python src/load_data.py`` as the table owner), so the web app can run
    as a least-privilege database user with only SELECT and INSERT.
    """
    if not raw_entries:
        return 0
    cleaned = clean.clean_data(raw_entries)
    with load_data.connect() as conn:
        inserted, _present, _unusable = load_data.load_records(conn, cleaned)
    return inserted


def default_search_fn(request: applicant_search.SearchRequest) -> list[dict]:
    """Production search for GET /api/applicants, in a read-only transaction.

    read_only=True makes PostgreSQL itself refuse any write in this session,
    a second line of defence behind the parameterized query.
    """
    with load_data.connect() as conn:
        conn.read_only = True
        return applicant_search.search_applicants(conn, request)
