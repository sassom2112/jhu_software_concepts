"""
test_db_insert.py - What a pull actually writes to PostgreSQL.

The Pull Data button no longer writes anything itself: it queues a
"scrape_new_data" task, and the worker's handler
(worker/consumer.handle_scrape_new_data) scrapes the new entries, cleans them
and inserts them.  Here Grad Café is fake (worker_site serves the listing
pages, no internet) and everything after it is real: the scraper, clean.py,
load_data.py and the applicants table of a throwaway *_test database, one
transaction per task, the way the worker runs it.
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from db.load_data import COLUMNS, count_rows, fetch_applicants
from worker.consumer import handle_scrape_new_data

# The columns every freshly pulled row must have filled in.  (The two llm_*
# columns stay NULL until the separate LLM step runs, so they are not here.)
REQUIRED = ("p_id", "program", "url", "date_added", "status", "term", "degree")


def pull(conn, payload=None) -> int:
    """One Pull Data task, the way the worker runs it: in a transaction of its own.
    Returns the number of rows inserted."""
    with conn.transaction():
        return handle_scrape_new_data(conn, payload or {})["inserted"]


@pytest.mark.db
def test_table_starts_empty(db_conn):
    assert count_rows(db_conn) == 0


@pytest.mark.db
def test_the_pull_data_button_only_queues_the_work(db_client, db_conn, fake_publisher):
    response = db_client.post("/pull-data")

    assert response.status_code == 202
    assert fake_publisher.kinds == ["scrape_new_data"]
    assert count_rows(db_conn) == 0                  # the web request itself wrote nothing


@pytest.mark.db
def test_pull_inserts_rows_with_required_fields(db_conn, worker_site):
    worker_site.serve_listing([[9_000_003, 9_000_002, 9_000_001]])
    assert count_rows(db_conn) == 0                  # before: the target table is empty

    inserted = pull(db_conn)

    assert inserted == 3
    rows = fetch_applicants(db_conn)
    assert [row["p_id"] for row in rows] == [9_000_003, 9_000_002, 9_000_001]
    for row in rows:
        missing = [column for column in REQUIRED if row[column] is None]
        assert missing == [], f"p_id {row['p_id']} is missing {missing}"


@pytest.mark.db
def test_pulled_row_is_cleaned_not_raw(db_conn, worker_site):
    worker_site.serve_listing([[9_000_001]])
    pull(db_conn)

    row = fetch_applicants(db_conn)[0]
    assert row["program"] == "Computer Science, Johns Hopkins University"
    assert row["date_added"] == date(2026, 9, 20)
    assert row["status"] == "Accepted"
    assert row["term"] == "Fall 2027"
    assert row["us_or_international"] == "International"
    assert row["gpa"] == 3.9
    assert row["llm_generated_program"] is None


@pytest.mark.db
def test_pulling_the_same_data_twice_adds_no_duplicates(db_conn, worker_site):
    worker_site.serve_listing([[9_000_003, 9_000_002, 9_000_001]])

    first = pull(db_conn)
    second = pull(db_conn)                           # the watermark: nothing newer is listed
    third = pull(db_conn, {"since": 0})              # all three read again: ON CONFLICT skips them

    assert first == 3
    assert second == third == 0
    assert count_rows(db_conn) == 3


@pytest.mark.db
def test_overlapping_pull_adds_only_the_new_row(db_conn, worker_site):
    worker_site.serve_listing([[9_000_002, 9_000_001]])
    pull(db_conn)
    worker_site.serve_listing([[9_000_003, 9_000_002, 9_000_001]])

    inserted = pull(db_conn)                         # two already stored + one new

    assert inserted == 1
    assert count_rows(db_conn) == 3


@pytest.mark.db
def test_fetch_applicants_returns_dicts_with_module3_keys(db_conn, worker_site):
    worker_site.serve_listing([[9_000_003, 9_000_002, 9_000_001]])
    pull(db_conn)

    rows = fetch_applicants(db_conn)

    assert len(rows) == 3
    for row in rows:
        assert isinstance(row, dict)
        assert set(row) == set(COLUMNS)


@pytest.mark.db
def test_schema_matches_module3(db_conn):
    columns = db_conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'applicants' ORDER BY ordinal_position"
    ).fetchall()
    primary_key = db_conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'applicants'::regclass AND i.indisprimary"
    ).fetchall()

    assert [name for name, _type in columns] == list(COLUMNS)
    assert dict(columns)["p_id"] == "integer"
    assert dict(columns)["date_added"] == "date"
    assert dict(columns)["gpa"] == "double precision"
    assert primary_key == [("p_id",)]


@pytest.mark.db
def test_failed_load_writes_nothing(db_conn, worker_site):
    # 2**40 does not fit PostgreSQL's INTEGER, so this one row makes the load fail.
    worker_site.serve_listing([[2**40, 9_000_003, 9_000_002, 9_000_001]])

    with pytest.raises(psycopg.Error):
        pull(db_conn)

    assert count_rows(db_conn) == 0               # the 3 good rows were rolled back too


@pytest.mark.db
def test_empty_scrape_inserts_nothing(db_conn, worker_site):
    worker_site.serve_listing([[]])

    assert pull(db_conn) == 0
    assert count_rows(db_conn) == 0
