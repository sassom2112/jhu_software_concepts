"""
test_load_data.py - The loader's edge cases and its command line.

test_db_insert.py covers the happy path through Pull Data.  These tests aim
at everything else load_data.py promises: odd values become NULL instead of
crashing, unusable records are counted and skipped, --reset starts over, and
every failure of the command line ends with a clear message and its own exit
code instead of a traceback.
"""

from __future__ import annotations

import runpy
import sys
from datetime import date

import pytest

import load_data
from load_data import COLUMNS, _date, _float, _p_id, count_rows, create_table, load_records, main, record_to_row
from scrape import save_data

pytestmark = pytest.mark.db


# --------------------------------------------------------------------------- #
#                  Value conversion (no database needed)                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("value", "expected"),
    [("3.5", 3.5), (4, 4.0), ("", None), (None, None), ("abc", None), ([3.5], None), ("nan", None)],
)
def test_float_turns_anything_unusable_into_none(value, expected):
    assert _float(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-20", date(2026, 9, 20)),
        ("2026-09-20T08:15:00", date(2026, 9, 20)),
        ("", None),
        (None, None),
        ("Sep 20, 2026", None),
    ],
)
def test_date_accepts_iso_dates_only(value, expected):
    assert _date(value) == expected


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"p_id": "12"}, 12),
        ({"result_id": 34}, 34),
        ({"result_id": "not a number", "url": "https://www.thegradcafe.com/result/56"}, 56),
        ({"url": "https://www.thegradcafe.com/result/56/"}, 56),
        ({"url": "https://www.thegradcafe.com/survey/"}, None),
        ({}, None),
    ],
)
def test_p_id_comes_from_the_id_fields_or_the_url(record, expected):
    assert _p_id(record) == expected


def test_record_without_an_id_is_unusable():
    assert record_to_row({"program": "Physics", "url": "https://www.thegradcafe.com/survey/"}) is None


def test_record_to_row_cleans_text_and_accepts_both_llm_spellings():
    row = dict(zip(COLUMNS, record_to_row({
        "p_id": 1,
        "program": "   ",
        "university": "MIT",
        "comments": "a\x00b",
        "llm_generated_program": "Physics",
        "llm-generated-university": "Massachusetts Institute of Technology",
    })))

    assert row["program"] == "MIT"                   # a blank program falls back to the university
    assert row["comments"] == "ab"                   # PostgreSQL text cannot store NUL characters
    assert row["llm_generated_program"] == "Physics"
    assert row["llm_generated_university"] == "Massachusetts Institute of Technology"


# --------------------------------------------------------------------------- #
#                        Database work (test database)                        #
# --------------------------------------------------------------------------- #

def test_load_records_skips_repeats_and_unusable_records(db_conn):
    records = [
        {"p_id": 1, "program": "first copy"},
        {"p_id": 1, "program": "second copy"},       # same id twice in one batch
        "not a dict",
        {"program": "no id anywhere"},
    ]

    with db_conn.transaction():                      # load_records leaves the transaction to its caller
        counts = load_records(db_conn, records)

    assert counts == (1, 0, 2)                       # (inserted, already present, unusable)
    assert db_conn.execute("SELECT program FROM applicants").fetchall() == [("first copy",)]


def test_create_table_reset_starts_over(db_conn):
    with db_conn.transaction():
        load_records(db_conn, [{"p_id": 1, "program": "Physics"}])

    create_table(db_conn, reset=True)

    assert count_rows(db_conn) == 0


# --------------------------------------------------------------------------- #
#                      Command line: python load_data.py                      #
# --------------------------------------------------------------------------- #

@pytest.fixture
def input_dir(tmp_path, monkeypatch):
    """Make load_data look for its input files in a temporary folder instead of src/."""
    monkeypatch.setattr(load_data, "HERE", tmp_path)
    return tmp_path


def test_main_loads_a_gzipped_file_and_reports_the_counts(db_conn, input_dir, capsys):
    records = [{"p_id": 1, "program": "Physics"}, {"p_id": 2, "program": "Chemistry"}, {"program": "no id"}]
    save_data(records, input_dir / "records.json.gz")

    exit_code = main(["--file", "records.json.gz"])

    assert exit_code == 0
    printed = capsys.readouterr().out
    assert "Read 3 records from records.json.gz" in printed
    assert "Inserted 2 new rows; 0 were already present; 1 unusable records skipped" in printed
    assert count_rows(db_conn) == 2


def test_main_reset_replaces_what_was_there(db_conn, input_dir):
    save_data([{"p_id": 1, "program": "Physics"}], input_dir / "first.json")
    save_data([{"p_id": 2, "program": "Chemistry"}], input_dir / "second.json")
    main(["--file", "first.json"])

    assert main(["--file", "second.json", "--reset"]) == 0
    assert db_conn.execute("SELECT p_id FROM applicants").fetchall() == [(2,)]


@pytest.mark.parametrize("name", ["missing.json", "..", "not_a_list.json"])
def test_main_reports_unreadable_input(input_dir, capsys, name):
    (input_dir / "not_a_list.json").write_text('{"p_id": 1}', encoding="utf-8")

    assert main(["--file", name]) == 1
    assert "error: cannot read input" in capsys.readouterr().err


def test_main_refuses_unparseable_settings(input_dir, capsys, monkeypatch):
    save_data([], input_dir / "empty.json")
    monkeypatch.setenv("DATABASE_URL", "dbname='unterminated")

    assert main(["--file", "empty.json"]) == 2
    printed = capsys.readouterr().err
    assert "connection settings are not valid" in printed
    assert "unterminated" not in printed             # the bad setting is never echoed back


def test_main_explains_an_unreachable_database(input_dir, capsys, monkeypatch):
    save_data([], input_dir / "empty.json")
    monkeypatch.setenv("DATABASE_URL", "postgresql://gradcafe@127.0.0.1:1/gradcafe_test")   # nothing listens on port 1

    assert main(["--file", "empty.json"]) == 2
    printed = capsys.readouterr().err
    assert "cannot connect to PostgreSQL at gradcafe@127.0.0.1:1/gradcafe_test" in printed
    assert "hint:" in printed


def test_main_rolls_back_a_failed_load(db_conn, input_dir, capsys):
    save_data([{"p_id": 1, "program": "Physics"}, {"p_id": 2**40, "program": "too big for INTEGER"}],
              input_dir / "bad.json")

    assert main(["--file", "bad.json"]) == 3
    assert "rolled back" in capsys.readouterr().err
    assert count_rows(db_conn) == 0                  # the good row was rolled back with the bad one


def test_running_load_data_py_exits_with_mains_code(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["load_data.py", "--file", "no-such-file.json"])

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("load_data", run_name="__main__")      # same as: python load_data.py ...

    assert stopped.value.code == 1