"""
test_snapshot.py - The stored analysis: what the worker writes and the web page reads.

The worker computes every answer with SQL (worker/etl/analytics.py) and stores
it as one JSON row (db/snapshot.py); the page only reads that row.  These tests
check the round trip through the *_test database, that the SQL snapshot says
exactly what the ORM-computed page used to say, that it can be computed inside
a transaction that also writes, and how the page looks with and without it.
"""

from __future__ import annotations

from datetime import date, datetime

import psycopg
import pytest
from bs4 import BeautifulSoup

from db.load_data import load_records, set_watermark
from db.snapshot import load_snapshot, save_snapshot, snapshot_status
from web.app import create_app
from worker.etl import orm_queries
from worker.etl.analytics import compute_snapshot, refresh_snapshot
from test_query_tools import sample_records   # the 300 synthetic applicants of the SQL-vs-ORM tests

pytestmark = [pytest.mark.analysis, pytest.mark.db]

PAYLOAD = {
    "summary": {"total_entries": 3, "newest_entry": "2026-09-15"},
    "answers": [{"number": "1", "question": "How many?", "lines": [["Count", "3"]],
                 "columns": [], "table": []}],
}


def save(conn, payload) -> datetime:
    """save_snapshot in a transaction of its own, the way the worker commits it."""
    with conn.transaction():
        return save_snapshot(conn, payload)


@pytest.fixture
def sample_data(db_conn):
    """The test database, filled with test_query_tools.sample_records()."""
    with db_conn.transaction():
        load_records(db_conn, sample_records())
    return db_conn


# --------------------------------------------------------------------------- #
#                       Saving and loading the one row                        #
# --------------------------------------------------------------------------- #

def test_nothing_is_stored_at_first(db_conn):
    assert load_snapshot(db_conn) is None
    assert snapshot_status(db_conn) is None


def test_a_saved_snapshot_loads_back_ready_for_the_page(db_conn):
    computed_at = save(db_conn, PAYLOAD)

    analysis = load_snapshot(db_conn)

    assert analysis["summary"] == {"total_entries": 3, "newest_entry": date(2026, 9, 15)}
    assert analysis["answers"] == PAYLOAD["answers"]
    assert analysis["computed_at"] == computed_at
    assert computed_at.tzinfo is not None                # a real point in time, not a local guess
    assert snapshot_status(db_conn) == {"computed_at": computed_at, "total_entries": 3}


def test_computed_at_is_when_the_snapshot_was_written_not_when_the_transaction_began(db_conn):
    # A pull's transaction starts before the scrape and the inserts that the snapshot then
    # counts, so the transaction's start time (now()) would date the analysis too early.
    with db_conn.transaction():
        began = db_conn.execute("SELECT now()").fetchone()[0]
        db_conn.execute("SELECT pg_sleep(0.05)")          # the work between the two
        computed_at = save_snapshot(db_conn, PAYLOAD)

    assert computed_at > began


def test_an_empty_database_has_no_newest_entry(db_conn):
    save(db_conn, {"summary": {"total_entries": 0, "newest_entry": None}, "answers": []})

    assert load_snapshot(db_conn)["summary"]["newest_entry"] is None


def test_saving_again_replaces_the_one_row(db_conn):
    first = save(db_conn, PAYLOAD)
    second = save(db_conn, {**PAYLOAD, "summary": {"total_entries": 4, "newest_entry": None}})

    assert db_conn.execute("SELECT count(*) FROM analysis_snapshot").fetchone() == (1,)
    assert second > first                                # what the page's poll notices
    assert snapshot_status(db_conn)["total_entries"] == 4


def test_the_table_holds_at_most_one_row(db_conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute("INSERT INTO analysis_snapshot (id, payload) VALUES (2, '{}')")


# --------------------------------------------------------------------------- #
#                  The SQL snapshot says what the ORM page said               #
# --------------------------------------------------------------------------- #

def orm_page(analysis: dict) -> dict:
    """orm_queries.get_analysis() in the snapshot's JSON shape: lists instead of tuples."""
    newest = analysis["summary"]["newest_entry"]
    return {
        "summary": {"total_entries": analysis["summary"]["total_entries"],
                    "newest_entry": newest.isoformat() if newest else None},
        "answers": [{"number": a.number, "question": a.question,
                     "lines": [list(line) for line in a.lines], "columns": list(a.columns),
                     "table": [list(row) for row in a.table]}
                    for a in analysis["answers"]],
    }


def test_the_sql_snapshot_matches_the_orm_answers(sample_data):
    snapshot = compute_snapshot(sample_data)

    assert snapshot == orm_page(orm_queries.get_analysis())
    assert snapshot["summary"] == {"total_entries": 300, "newest_entry": "2026-09-15"}
    assert len(snapshot["answers"]) == 11


def test_the_sql_snapshot_matches_the_orm_on_an_empty_table(db_conn):
    snapshot = compute_snapshot(db_conn)

    assert snapshot == orm_page(orm_queries.get_analysis())
    assert snapshot["summary"] == {"total_entries": 0, "newest_entry": None}


def test_the_page_renders_the_snapshot_exactly_as_it_rendered_the_orm_answers(sample_data, db_client):
    with sample_data.transaction():
        refresh_snapshot(sample_data)
    orm_client = create_app(publish_fn=lambda kind: None, query_fn=orm_queries.get_analysis).test_client()

    snapshot_page = BeautifulSoup(db_client.get("/analysis").data, "html.parser")
    orm_page_html = BeautifulSoup(orm_client.get("/analysis").data, "html.parser")

    assert str(snapshot_page.select_one("section.grid")) == str(orm_page_html.select_one("section.grid"))
    stats = [s.get_text(strip=True) for s in snapshot_page.select(".stat-value")]
    assert stats[:2] == [s.get_text(strip=True) for s in orm_page_html.select(".stat-value")][:2]
    assert stats[:2] == ["300", "Sep 15, 2026"]


# --------------------------------------------------------------------------- #
#             Computed inside the worker's read-write transaction             #
# --------------------------------------------------------------------------- #

def test_the_snapshot_is_refreshed_in_the_same_transaction_that_writes(db_conn):
    with db_conn.transaction():
        load_records(db_conn, sample_records(10))
        payload = refresh_snapshot(db_conn)
        set_watermark(db_conn, "100009")      # a write AFTER the analysis: not a read-only transaction

    assert payload["summary"]["total_entries"] == 10
    assert load_snapshot(db_conn)["summary"]["total_entries"] == 10


def test_a_rolled_back_transaction_leaves_the_old_snapshot(db_conn):
    first = save(db_conn, PAYLOAD)

    with pytest.raises(RuntimeError), db_conn.transaction():
        load_records(db_conn, sample_records(10))
        refresh_snapshot(db_conn)
        raise RuntimeError("the handler failed after refreshing")

    assert snapshot_status(db_conn) == {"computed_at": first, "total_entries": 3}


# --------------------------------------------------------------------------- #
#                         The page, with and without one                      #
# --------------------------------------------------------------------------- #

@pytest.mark.web
def test_the_page_shows_the_stored_analysis_and_when_it_was_computed(sample_data, db_client):
    with sample_data.transaction():
        refresh_snapshot(sample_data)
    computed_at = snapshot_status(sample_data)["computed_at"]

    response = db_client.get("/analysis")
    soup = BeautifulSoup(response.data, "html.parser")

    assert response.status_code == 200
    assert len(soup.select("article.card")) == 11
    assert "Analysis computed at" in soup.get_text()
    assert soup.select_one("[data-testid=computed-at]").get_text(strip=True) == \
        computed_at.strftime("%B %d, %Y at %I:%M:%S %p %Z").strip()
    assert soup.select_one("#analysis-page")["data-computed-at"] == computed_at.isoformat()


@pytest.mark.web
def test_without_a_snapshot_the_page_explains_what_to_do(db_client):
    response = db_client.get("/analysis")
    soup = BeautifulSoup(response.data, "html.parser")

    assert response.status_code == 200                   # a friendly page, not an error
    notice = soup.select_one("[data-testid=no-analysis]")
    assert "No analysis has been computed yet" in notice.get_text()
    assert notice["role"] == "status"
    assert soup.find(attrs={"role": "alert"}) is None     # nothing is wrong
    assert soup.find(attrs={"data-testid": "update-analysis-btn"}) is not None
    assert soup.select_one("#analysis-page")["data-computed-at"] == ""
