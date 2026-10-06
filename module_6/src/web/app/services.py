"""
services.py - The production defaults behind the page, the status poll and the search API.

Each function opens its own short connection with the environment's settings
(db_config: DATABASE_URL, or DB_* naming the read-only web role), marks it
read-only, runs one LIMITed SELECT and closes it.  read_only=True makes
PostgreSQL itself refuse any write in that session: a second line of defence
behind the web role, which may only SELECT anyway.

  * default_query_fn: the stored analysis snapshot (db/snapshot.py) for
    GET /analysis, or None when the worker has not computed one yet.
  * default_status_fn: just its computed_at and entry count, for the page's
    "has the worker finished?" poll (GET /api/analysis-status).
  * default_search_fn: GET /api/applicants, the one path where user input
    reaches SQL (see applicant_search.py for the injection defences).

Nothing here scrapes, inserts or recomputes: that is the worker's job, asked
for through RabbitMQ (web/publisher.py).  Importing this module never needs a
live database; each function connects only when it runs.
"""

from __future__ import annotations

import psycopg

from db import load_data, snapshot
from web.app import applicant_search


def _read_only_connection() -> psycopg.Connection:
    """A new connection that PostgreSQL will not let write anything."""
    conn = load_data.connect()
    conn.read_only = True
    return conn


def default_query_fn() -> dict | None:
    """The analysis page's data: the stored snapshot, or None if there is none yet."""
    with _read_only_connection() as conn:
        return snapshot.load_snapshot(conn)


def default_status_fn() -> dict | None:
    """{"computed_at", "total_entries"} of the stored snapshot, or None (one small row)."""
    with _read_only_connection() as conn:
        return snapshot.snapshot_status(conn)


def default_search_fn(request: applicant_search.SearchRequest) -> list[dict]:
    """Production search for GET /api/applicants, in a read-only transaction."""
    with _read_only_connection() as conn:
        return applicant_search.search_applicants(conn, request)
