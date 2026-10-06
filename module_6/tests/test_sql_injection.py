"""
test_sql_injection.py - Attacking GET /api/applicants the way an attacker would.

This endpoint is the one place where text typed by a user reaches SQL.  Each
test sends hostile input through the real route, the real validation
(applicant_search.parse_search_args) and a real query against the *_test
database, then checks three things: nothing crashed, nothing leaked, and the
table is still intact.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

import applicant_search
from applicant_search import SearchRequest, build_search_query
from load_data import count_rows, load_records
from query_limits import MAX_LIMIT, LimitError, clamp_limit
from webapp import create_app

pytestmark = [pytest.mark.db, pytest.mark.web]


def applicant(p_id: int, **fields) -> dict:
    """One stored row; override any field by keyword."""
    record = {
        "p_id": p_id, "program": "Computer Science, Johns Hopkins University",
        "term": "Fall 2026", "status": "Accepted", "degree": "PhD",
        "us_or_international": "American", "gpa": 3.5, "date_added": "2026-09-20",
        "url": f"https://www.thegradcafe.com/result/{p_id}",
    }
    record.update(fields)
    return record


@pytest.fixture
def stored(db_conn):
    """Five known rows in the test database; returns the connection for checks."""
    rows = [
        applicant(1, gpa=3.9),
        applicant(2, gpa=3.1, status="Rejected"),
        applicant(3, gpa=3.5, term="Fall 2025"),
        applicant(4, gpa=3.7, us_or_international="International"),
        applicant(5, gpa=2.9, program="Physics, MIT"),
    ]
    with db_conn.transaction():
        load_records(db_conn, rows)
    return db_conn


def search(client, **params):
    """GET /api/applicants with the given query-string parameters."""
    response = client.get("/api/applicants", query_string=params)
    return response.status_code, response.get_json()


# --------------------------------------------------------------------------- #
#                      The statement: values are never SQL text               #
# --------------------------------------------------------------------------- #

def test_values_are_bound_parameters_not_sql_text():
    request = SearchRequest(filters={"term": "' OR 1=1 --"}, sort="gpa", order="asc", limit=5)

    statement, params = build_search_query(request)
    text = statement.as_string()

    assert "' OR 1=1" not in text                         # the attack is nowhere in the SQL...
    assert "%(term)s" in text                             # ...only a placeholder is
    assert params == {"term": "' OR 1=1 --", "limit": 5}  # ...and the value travels separately
    assert '"gpa" ASC' in text                            # the column is a quoted identifier
    assert text.endswith("LIMIT %(limit)s")               # and every query ends in a LIMIT


# --------------------------------------------------------------------------- #
#                         Normal use still works                              #
# --------------------------------------------------------------------------- #

def test_a_normal_search_filters_sorts_and_limits(stored, db_client):
    status, body = search(db_client, term="Fall 2026", status="Accepted", sort="gpa", order="desc")

    assert status == 200
    assert [row["p_id"] for row in body["rows"]] == [1, 4, 5]
    assert body["rows"][0]["date_added"] == "2026-09-20"  # dates come back as ISO strings
    assert body["limit"] == 20                            # the default when none is asked for


# --------------------------------------------------------------------------- #
#                   Attacks through a filter value: just text                 #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "payload",
    [
        "' OR '1'='1",
        "Fall 2026' OR '1'='1' --",
        "Fall 2026'; DROP TABLE applicants; --",
        "' UNION SELECT usename, passwd, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL "
        "FROM pg_shadow --",
    ],
    ids=["always-true", "comment-out-the-rest", "stacked-drop-table", "union-steal-passwords"],
)
def test_injection_in_a_filter_value_matches_nothing(stored, db_client, payload):
    status, body = search(db_client, term=payload)

    assert status == 200                                  # no crash
    assert body["rows"] == []                             # no leak: no term is literally that text
    assert count_rows(stored) == 5                        # and nothing was dropped or changed


def test_like_wildcards_in_program_are_literal(stored, db_client):
    assert search(db_client, program="%")[1]["rows"] == []   # "%" does not mean "everything"
    assert search(db_client, program="_")[1]["rows"] == []
    assert [row["p_id"] for row in search(db_client, program="physics")[1]["rows"]] == [5]


# --------------------------------------------------------------------------- #
#             Attacks through names and directions: refused outright          #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("sort", "p_id; DROP TABLE applicants"),
        ("sort", "gpa DESC, (SELECT 1)"),
        ("sort", "comments"),                                 # a real column, but not sortable
        ("order", "desc; DROP TABLE applicants"),
        ("order", "sideways"),
    ],
)
def test_names_and_directions_outside_the_allow_list_are_refused(stored, db_client, name, value):
    status, body = search(db_client, **{name: value})

    assert status == 400
    assert body["ok"] is False
    assert count_rows(stored) == 5


# --------------------------------------------------------------------------- #
#                   LIMIT: always present, always clamped                     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("asked", "limit"),
    [("0", 1), ("-5", 1), ("3", 3), ("1000000", MAX_LIMIT), ("99999999999999999999", MAX_LIMIT)],
)
def test_limit_is_clamped_between_1_and_max(stored, db_client, asked, limit):
    status, body = search(db_client, limit=asked)

    assert status == 200
    assert body["limit"] == limit
    assert len(body["rows"]) <= limit


@pytest.mark.parametrize("asked", ["abc", "5.5", "1e9", "0x10", "10; DROP TABLE applicants"])
def test_a_limit_that_is_not_a_whole_number_is_refused(stored, db_client, asked):
    status, body = search(db_client, limit=asked)

    assert status == 400
    assert "whole number" in body["error"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 20), ("", 20), (" 7 ", 7), (7, 7), ("+3", 3), ("-9999999", 1), ("12345678901", 100)],
)
def test_clamp_limit(value, expected):
    assert clamp_limit(value) == expected


@pytest.mark.parametrize("value", [True, 5.5, "abc", "1e9", "\uff15"])   # \uff15 is a full-width 5
def test_clamp_limit_refuses_anything_but_a_plain_whole_number(value):
    with pytest.raises(LimitError):
        clamp_limit(value)


# --------------------------------------------------------------------------- #
#                  Oversized or impossible input: refused early               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value", ["x" * 101, "Fall\x002026"], ids=["too-long", "nul-byte"])
def test_oversized_or_impossible_filter_values_are_refused(stored, db_client, value):
    status, body = search(db_client, term=value)

    assert status == 400
    assert body["ok"] is False


# --------------------------------------------------------------------------- #
#            Defence in depth: no details leak, no writes are possible        #
# --------------------------------------------------------------------------- #

def test_database_errors_are_not_shown_to_the_caller():
    def failing_search(_request):
        raise psycopg.OperationalError('password authentication failed for user "gradcafe_app"')

    client = create_app(search_fn=failing_search, query_fn=lambda: {}).test_client()

    response = client.get("/api/applicants")

    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": "the search could not be completed"}
    assert "gradcafe_app" not in response.get_data(as_text=True)


def test_the_search_connection_is_read_only(stored, db_client, monkeypatch):
    def write_instead(conn, _request):
        conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier("applicants")))

    monkeypatch.setattr(applicant_search, "search_applicants", write_instead)

    status, _body = search(db_client, term="Fall 2026")

    assert status == 500                                  # PostgreSQL refused the write...
    assert count_rows(stored) == 5                        # ...so every row is still there