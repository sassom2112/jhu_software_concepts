"""
analytics.py - Recompute the analysis the web page shows and store it as the snapshot.

The web app no longer runs the analysis queries: it reads one stored row,
analysis_snapshot (see db/snapshot.py).  This module fills that row.
compute_snapshot() runs the summary and all eleven questions with the same
handwritten SQL as query_data.py, on the caller's connection, and returns them
as plain JSON; refresh_snapshot() also saves the result.

Both leave the transaction to the caller, and neither sends SET TRANSACTION
READ ONLY, so the worker can recompute and store the snapshot inside the same
per-message transaction that inserted new rows: either the rows and the
updated analysis are committed together, or neither is.

Usage from a task handler (conn is a psycopg connection as the worker role)::

    with conn.transaction():
        ...insert new rows...
        refresh_snapshot(conn)
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from db.snapshot import save_snapshot
from worker.etl.query_data import Answer, answer_all, database_summary


def answer_payload(answer: Answer) -> dict:
    """What the page shows of one answer, as JSON-ready lists (no SQL, no explanation)."""
    return {
        "number": answer.number,
        "question": answer.question,
        "lines": [list(line) for line in answer.lines],
        "columns": list(answer.columns),
        "table": [list(row) for row in answer.table],
    }


def compute_snapshot(conn: psycopg.Connection) -> dict:
    """The page's summary and every answer, computed with SQL on *conn*, as plain JSON.

    newest_entry becomes an ISO date string ("2026-09-15"), or None for an empty
    table; every answer value is already a formatted string.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        summary = database_summary(cur)
        answers = answer_all(cur)
    newest = summary["newest_entry"]
    return {
        "summary": {
            "total_entries": summary["total_entries"],
            "newest_entry": newest.isoformat() if newest else None,
        },
        "answers": [answer_payload(answer) for answer in answers],
    }


def refresh_snapshot(conn: psycopg.Connection) -> dict:
    """Compute the snapshot on *conn* and save it (analysis_snapshot, id 1) in the
    caller's transaction.  Returns the payload that was stored."""
    payload = compute_snapshot(conn)
    save_snapshot(conn, payload)
    return payload
