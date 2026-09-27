"""
test_db_insert.py - What "Pull Data" actually writes to PostgreSQL.

Every other test file swaps the database out for FakeLoader.  Here only the
scraper is fake (no internet); the loader is the real services.default_load_fn,
so rows really go through clean.py -> load_data.py -> the applicants table of
a throwaway *_test database.
"""

from __future__ import annotations

from datetime import date

import pytest

from load_data import COLUMNS, count_rows, fetch_applicants

# The columns every freshly pulled row must have filled in.  (The two llm_*
# columns stay NULL until the separate LLM step runs, so they are not here.)
REQUIRED = ("p_id", "program", "url", "date_added", "status", "term", "degree")


@pytest.mark.db
def test_table_starts_empty(db_conn):
    assert count_rows(db_conn) == 0


@pytest.mark.db
def test_pull_data_inserts_rows_with_required_fields(db_client, db_conn, fake_scraper, raw_entries):
    fake_scraper.rows = raw_entries

    response = db_client.post("/pull-data")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "inserted": 3}
    rows = fetch_applicants(db_conn)
    assert [row["p_id"] for row in rows] == [9_000_003, 9_000_002, 9_000_001]
    for row in rows:
        missing = [column for column in REQUIRED if row[column] is None]
        assert missing == [], f"p_id {row['p_id']} is missing {missing}"


@pytest.mark.db
def test_pulled_row_is_cleaned_not_raw(db_client, db_conn, fake_scraper, raw_entries):
    fake_scraper.rows = raw_entries[:1]

    db_client.post("/pull-data")

    row = fetch_applicants(db_conn)[0]
    assert row["program"] == "Computer Science, Johns Hopkins University"
    assert row["date_added"] == date(2026, 9, 20)
    assert row["status"] == "Accepted"
    assert row["term"] == "Fall 2027"
    assert row["us_or_international"] == "International"
    assert row["gpa"] == 3.9
    assert row["llm_generated_program"] is None


@pytest.mark.db
def test_pulling_the_same_data_twice_adds_no_duplicates(db_client, db_conn, fake_scraper, raw_entries):
    fake_scraper.rows = raw_entries

    first = db_client.post("/pull-data").get_json()
    second = db_client.post("/pull-data").get_json()

    assert first["inserted"] == 3
    assert second["inserted"] == 0
    assert count_rows(db_conn) == 3


@pytest.mark.db
def test_overlapping_pull_adds_only_the_new_row(db_client, db_conn, fake_scraper, raw_entries):
    fake_scraper.rows = raw_entries[:2]
    db_client.post("/pull-data")

    fake_scraper.rows = raw_entries          # two already stored + one new
    response = db_client.post("/pull-data")

    assert response.get_json()["inserted"] == 1
    assert count_rows(db_conn) == 3


@pytest.mark.db
def test_fetch_applicants_returns_dicts_with_module3_keys(db_client, db_conn, fake_scraper, raw_entries):
    fake_scraper.rows = raw_entries
    db_client.post("/pull-data")

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
def test_failed_load_writes_nothing(db_client, db_conn, fake_scraper, raw_entries):
    # 2**40 does not fit PostgreSQL's INTEGER, so this one row makes the load fail.
    too_big = 2**40
    fake_scraper.rows = raw_entries + [
        dict(raw_entries[0], result_id=too_big, url=f"https://www.thegradcafe.com/result/{too_big}")
    ]

    response = db_client.post("/pull-data")

    assert response.status_code == 500
    assert response.get_json()["ok"] is False
    assert count_rows(db_conn) == 0               # the 3 good rows were rolled back too
    assert db_client.application.pull_state.is_running is False


@pytest.mark.db
def test_empty_scrape_inserts_nothing(db_client, db_conn, fake_scraper):
    fake_scraper.rows = []

    response = db_client.post("/pull-data")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "inserted": 0}
    assert count_rows(db_conn) == 0