"""
test_clean.py - Turning raw scraper text into the fields the database stores.

clean.py decides what lands in every column test_db_insert.py checks, so it
belongs to the data layer (marked db).  Most of its work is small parsers;
each gets a parametrized table of (input, expected) pairs, including the
awkward cases the real site produces: badges with no year, a leap day,
placeholder "0" badges, and entries whose visible text is missing entirely.
"""

from __future__ import annotations

import runpy
import sys

import pytest

import clean
from clean import (_classify_tags, _clean_text, _normalize_status, _parse_applicant_type, _parse_date_added,
                   _parse_decision, _parse_iso_date, _parse_term, _resolve_decision_date, _to_number,
                   _to_number_or_none, clean_data, main)
from scrape import load_data, save_data

pytestmark = pytest.mark.db


# --------------------------------------------------------------------------- #
#                     Whole entries through clean_data()                      #
# --------------------------------------------------------------------------- #

def test_page_json_fills_in_when_the_visible_text_is_missing():
    entry = {
        "result_id": 5,
        "url": "https://www.thegradcafe.com/result/5",
        "tags_text": [],
        "listing_json": {
            "school": "MIT", "program": "Physics", "level": "PhD",
            "created_at": "2026-07-10T00:00:00.000000Z", "decision": "accepted",
            "date_of_notification": "2026-07-01", "season": "fall 2026", "status": "International",
            "ugpa": "3.80", "greq": "165", "grev": "0", "grew": "4.5", "notes": "From the page JSON",
        },
    }

    record = clean_data([entry])[0]

    assert record["program"] == "Physics, MIT"
    assert record["degree"] == "PhD"
    assert record["date_added"] == "2026-07-10"
    assert record["status"] == "Accepted"
    assert (record["decision_date"], record["decision_date_source"]) == ("2026-07-01", "site_json")
    assert record["term"] == "Fall 2026"
    assert record["us_or_international"] == "International"
    assert (record["gpa"], record["gre"], record["gre_v"], record["gre_aw"]) == (3.8, 165, None, 4.5)
    assert record["comments"] == "From the page JSON"


def test_an_entry_with_almost_nothing_stays_empty_instead_of_guessing():
    record = clean_data([{"result_id": 6, "program_text": "Physics", "tags_text": ["Visa pending"]}])[0]

    assert record["program"] is None                 # no university, so no "Program, University"
    assert record["status"] is None
    assert (record["decision_date"], record["decision_date_source"]) == (None, None)
    assert record["other_tags"] == ["Visa pending"]


def test_badges_are_sorted_into_typed_fields():
    tags = ["0", "GRE AW 4.5", "GRE V 160", "GRE 165", "GPA 3.90",
            "Accepted on Sep 18", "Fall 2026", "American", "Visa pending"]

    assert _classify_tags(tags) == {
        "term": "Fall 2026",
        "applicant_type": "American",
        "gpa": 3.9,
        "gre": 165,
        "gre_verbal": 160,
        "gre_analytical_writing": 4.5,
        "other_tags": ["Visa pending"],             # "0" and the repeated decision badge are dropped
    }


# --------------------------------------------------------------------------- #
#                          One parser, one table                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("decision_text", "date_added", "expected"),
    [
        ("Accepted on Sep 18", "2026-09-20", ("Accepted", "2026-09-18")),
        ("Wait listed on Sep 18", "2026-09-20", ("Waitlisted", "2026-09-18")),
        ("Other", "2026-09-20", ("Other", None)),
        ("Pending review", "2026-09-20", ("Pending Review", None)),
        (None, "2026-09-20", (None, None)),
    ],
)
def test_parse_decision(decision_text, date_added, expected):
    assert _parse_decision(decision_text, date_added) == expected


@pytest.mark.parametrize(
    ("badge", "date_added", "expected"),
    [
        ("Sep 18", "2026-09-20", "2026-09-18"),
        ("Dec 30", "2026-01-05", "2025-12-30"),      # after the date added, so it was last year
        ("18 Sep", "2026-09-20", "2026-09-18"),
        ("Sep 18, 2026", None, "2026-09-18"),        # a full date needs no reference year
        ("Sep 18", None, None),                      # no year to infer from
        ("sometime", "2026-09-20", None),
        ("Feb 29", "2028-03-01", "2028-02-29"),
        ("Feb 29", "2027-03-01", None),              # 2027 has no Feb 29
    ],
)
def test_resolve_decision_date(badge, date_added, expected):
    assert _resolve_decision_date(badge, date_added) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Sep 11, 2026", "2026-09-11"), ("September 11, 2026", "2026-09-11"), ("yesterday", None), (None, None)],
)
def test_parse_date_added(text, expected):
    assert _parse_date_added(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-07-10T00:00:00.000000Z", "2026-07-10"), ("not a date", None), (20260710, None), (None, None)],
)
def test_parse_iso_date(value, expected):
    assert _parse_iso_date(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("3.80", 3.8), (165, 165), ("0.00", None), ("", None), (None, None), ("n/a", None)],
)
def test_to_number_or_none_treats_zero_as_missing(value, expected):
    assert _to_number_or_none(value) == expected


@pytest.mark.parametrize(
    ("text", "expected", "kind"),
    [("163", 163, int), ("3.40", 3.4, float), ("4.00", 4.0, float), ("abc", None, type(None))],
)
def test_to_number_keeps_the_decimal_point(text, expected, kind):
    assert _to_number(text) == expected
    assert type(_to_number(text)) is kind


@pytest.mark.parametrize(
    ("parser", "text", "expected"),
    [
        (_parse_term, "fall 2026", "Fall 2026"),
        (_parse_term, "Fall", None),
        (_parse_term, None, None),
        (_parse_applicant_type, "international", "International"),
        (_parse_applicant_type, "Martian", None),
        (_parse_applicant_type, None, None),
        (_normalize_status, "Wait listed", "Waitlisted"),
        (_normalize_status, "pending review", "Pending Review"),
        (_normalize_status, None, None),
    ],
)
def test_small_parsers(parser, text, expected):
    assert parser(text) == expected


@pytest.mark.parametrize(
    ("value", "keep_newlines", "expected"),
    [
        ("  Johns   Hopkins ", False, "Johns Hopkins"),
        ("line one\n\n  line   two", True, "line one\nline two"),
        ("   ", False, None),
        (None, False, None),
    ],
)
def test_clean_text_collapses_whitespace_only(value, keep_newlines, expected):
    assert _clean_text(value, keep_newlines=keep_newlines) == expected


# --------------------------------------------------------------------------- #
#                       Command line: python clean.py                         #
# --------------------------------------------------------------------------- #

@pytest.fixture
def work_dir(tmp_path, monkeypatch):
    """Run clean.py's command line inside a temporary folder instead of src/."""
    monkeypatch.setattr(clean, "HERE", tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path


def test_main_cleans_the_gzipped_raw_file_by_default(work_dir, capsys, raw_entries):
    save_data(raw_entries, work_dir / "data" / "raw_entries.json.gz")

    assert main([]) == 0

    cleaned = load_data(work_dir / "applicant_data.json")
    assert [record["result_id"] for record in cleaned] == [9_000_003, 9_000_002, 9_000_001]   # newest first
    assert "Cleaned 3 entries" in capsys.readouterr().out


def test_main_accepts_input_and_output_names(work_dir, raw_entries):
    save_data(raw_entries[:1], work_dir / "data" / "some_raw.json")

    assert main(["--input", "some_raw.json", "--output", "some_clean.json"]) == 0
    assert len(load_data(work_dir / "some_clean.json")) == 1


def test_main_refuses_a_path_instead_of_a_name(work_dir, capsys):
    assert main(["--output", ".."]) == 1
    assert "error:" in capsys.readouterr().err


def test_running_clean_py_exits_with_mains_code(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["clean.py", "--output", ".."])

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("clean", run_name="__main__")

    assert stopped.value.code == 1