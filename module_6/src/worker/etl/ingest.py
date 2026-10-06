"""
ingest.py - Store freshly scraped Grad Café entries: clean them, then insert the new ones.

The worker's "scrape_new_data" task fetches raw entries from Grad Café
(scrape.py) and hands them to insert_scraped_entries(), which

  1. cleans them with clean.clean_data (the Module 2 cleaning rules: program
     and university joined, dates as ISO, GPA and GRE parsed, ...), and
  2. inserts them with db.load_data.load_records: a parameterized bulk COPY
     into a temporary table, then INSERT ... ON CONFLICT (p_id) DO NOTHING, so
     an entry that is already stored is skipped, never duplicated or changed.

Standardizing program and university names with the local LLM is a separate,
much slower step; new rows land with llm_generated_program and
llm_generated_university left NULL, like any other not-yet-standardized row.

Nothing here commits: the caller's transaction decides, so the rows, the
watermark and the refreshed analysis snapshot are committed together.  It
never creates or alters a table either, so it runs as the worker role, which
may only SELECT and INSERT on applicants.
"""

from __future__ import annotations

from collections.abc import Sequence

import psycopg

from db.load_data import load_records
from worker.etl import clean


def insert_scraped_entries(conn: psycopg.Connection, raw_entries: Sequence[dict]) -> int:
    """Clean *raw_entries* (as GradCafeScraper returns them) and insert the ones not stored
    yet, inside the caller's transaction.  Returns the number of rows inserted."""
    if not raw_entries:
        return 0
    inserted, _present, _unusable = load_records(conn, clean.clean_data(list(raw_entries)))
    return inserted
