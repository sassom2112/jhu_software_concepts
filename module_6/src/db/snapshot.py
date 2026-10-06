"""
snapshot.py - The analysis the web page shows, stored as one row of JSON.

The worker computes every analysis answer with SQL (worker/etl/analytics.py)
and saves the result here with save_snapshot(); the web app reads it back with
load_snapshot() on every page view, and polls snapshot_status() to notice when
the worker has saved a newer one.  So the web app never runs the analysis
itself, needs only SELECT on this table, and imports nothing from the worker.

The table (created by load_data.create_schema) holds at most one row:

    analysis_snapshot (id SMALLINT PRIMARY KEY CHECK (id = 1),
                       computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                       payload JSONB NOT NULL)

The payload is plain JSON::

    {"summary": {"total_entries": 30500, "newest_entry": "2026-09-15"},
     "answers": [{"number": "1", "question": "...", "lines": [["label", "value"]],
                  "columns": [], "table": []}, ...]}

Dates travel as ISO strings; load_snapshot() turns newest_entry back into a
datetime.date and adds the row's computed_at, so the page can format both.
"""

from __future__ import annotations

from datetime import date, datetime

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from db.db_config import SNAPSHOT_TABLE

SNAPSHOT_ID = 1   # the CHECK constraint allows exactly this one row

_NAMES = {"table": sql.Identifier(SNAPSHOT_TABLE), "id": sql.Placeholder("id")}

# INSERT the row the first time, replace its payload every time after that.
# computed_at is statement_timestamp(): when this INSERT started, right after the analysis
# queries read the data.  (now() would be the start of the caller's transaction, which for
# a pull is before the scrape and the inserts the snapshot already counts.)
SAVE_SNAPSHOT_STATEMENT = sql.SQL(
    "INSERT INTO {table} (id, computed_at, payload) "
    "VALUES ({id}, statement_timestamp(), {payload}) "
    "ON CONFLICT (id) DO UPDATE SET computed_at = EXCLUDED.computed_at, "
    "payload = EXCLUDED.payload "
    "RETURNING computed_at"
).format(payload=sql.Placeholder("payload"), **_NAMES)
LOAD_SNAPSHOT_STATEMENT = sql.SQL(
    "SELECT computed_at, payload FROM {table} WHERE id = {id} LIMIT 1"
).format(**_NAMES)
# What the page polls: two small values, never the whole payload.
SNAPSHOT_STATUS_STATEMENT = sql.SQL(
    "SELECT computed_at, (payload -> 'summary' ->> 'total_entries')::bigint AS total_entries "
    "FROM {table} WHERE id = {id} LIMIT 1"
).format(**_NAMES)


def save_snapshot(conn: psycopg.Connection, payload: dict) -> datetime:
    """Store *payload* as the analysis snapshot; returns its new computed_at.

    *payload* must already be JSON-safe (dates as ISO strings).  The caller
    controls the transaction, so the snapshot commits together with the data
    it was computed from, or not at all.
    """
    with conn.cursor() as cur:
        cur.execute(SAVE_SNAPSHOT_STATEMENT, {"id": SNAPSHOT_ID, "payload": Jsonb(payload)})
        return cur.fetchone()[0]


def load_snapshot(conn: psycopg.Connection) -> dict | None:
    """The stored analysis, ready for the page, or None when none has been computed yet.

    The result is the payload plus "computed_at" (a timezone-aware datetime),
    with summary.newest_entry turned back into a datetime.date (None stays None).
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(LOAD_SNAPSHOT_STATEMENT, {"id": SNAPSHOT_ID})
        row = cur.fetchone()
    if row is None:
        return None
    analysis = dict(row["payload"])
    summary = dict(analysis.get("summary") or {})
    newest = summary.get("newest_entry")
    summary["newest_entry"] = date.fromisoformat(newest) if newest else None
    analysis["summary"] = summary
    analysis["computed_at"] = row["computed_at"]
    return analysis


def snapshot_status(conn: psycopg.Connection) -> dict | None:
    """{"computed_at": datetime, "total_entries": int} of the stored snapshot, or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SNAPSHOT_STATUS_STATEMENT, {"id": SNAPSHOT_ID})
        return cur.fetchone()
