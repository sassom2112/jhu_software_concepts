"""
test_query_tools.py - The analysis outside the web page: SQL, ORM and the PDF report.

Module 3 answers the same eleven questions two ways -- handwritten SQL in
query_data.py and the SQLAlchemy ORM in orm_queries.py -- and promises they
agree.  The first tests hold both to that promise on a synthetic data set
large enough to fill every question, including Question 10's 100-entry cutoff.
The rest cover the three command-line tools: their output, and the exit code
each one gives when the database is misconfigured, down, or missing its table.
"""

from __future__ import annotations

import runpy
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import build_query_results
import orm_queries
import query_data
from load_data import load_records
from models import SessionLocal

pytestmark = [pytest.mark.analysis, pytest.mark.db]      # a test may carry more than one marker

# (school as typed in the original program text, canonical name from the LLM standardizer)
SCHOOLS = [
    ("Johns Hopkins University", "Johns Hopkins University"),
    ("Stanford University", "Stanford University"),
    ("MIT", "Massachusetts Institute of Technology"),
    ("Georgetown University", "Georgetown University"),
    ("CMU", "Carnegie Mellon University"),
    ("Some Other College", "Unknown"),
]


def sample_records(count: int = 300) -> list[dict]:
    """Deterministic applicants that give every question a non-empty answer.

    Some scores are deliberately off their official scale (a GPA of 0, a GRE
    "Quantitative" of 320) so both query styles must apply the same filters.
    """
    records = []
    for i in range(count):
        school, llm_school = SCHOOLS[i % len(SCHOOLS)]
        records.append({
            "p_id": 100_000 + i,
            "program": f"Computer Science, {school}",
            "degree": "PhD" if i % 2 else "Masters",
            "term": "Fall 2025" if i % 5 == 0 else "Fall 2026",
            "status": ("Accepted", "Rejected", "Wait listed")[i % 3],
            "us_or_international": ("American", "International", "Other", "International")[i % 4],
            "date_added": f"2026-{1 + i % 9:02d}-15",
            "gpa": 3.0 + (i % 11) / 10 if i % 7 else 0.0,
            "gre": 150 + i % 21 if i % 9 else 320,
            "gre_v": 140 + i % 31,
            "gre_aw": (i % 13) / 2,
            "llm_generated_program": "Computer Science",
            "llm_generated_university": llm_school,
        })
    return records


@pytest.fixture
def sample_data(db_conn):
    """The test database, filled with sample_records()."""
    with db_conn.transaction():
        load_records(db_conn, sample_records())
    return db_conn


def answers_both_ways(conn):
    """(SQL answers, ORM answers), each reduced to what the page and console show."""
    def shown(answer):
        return answer.number, answer.lines, answer.columns, answer.table

    sql_answers = [shown(a) for a in query_data.run_all(conn)]
    with SessionLocal() as session:
        orm_answers = [shown(a) for a in orm_queries.build_answers(session)]
    return sql_answers, orm_answers


# --------------------------------------------------------------------------- #
#                        SQL and ORM give the same answers                    #
# --------------------------------------------------------------------------- #

def test_sql_and_orm_agree_on_every_question(sample_data):
    sql_answers, orm_answers = answers_both_ways(sample_data)

    assert sql_answers == orm_answers
    by_number = {number: (lines, table) for number, lines, _columns, table in sql_answers}
    assert by_number["1"][0] == [("Fall 2026 applicant count", "240")]
    assert len(by_number["10"][1]) == 2                  # both degrees clear the 100-entry cutoff
    assert len(by_number["11"][1]) == 5                  # five schools; "Unknown" is left out


def test_sql_and_orm_agree_on_an_empty_table(db_conn):
    sql_answers, orm_answers = answers_both_ways(db_conn)

    assert sql_answers == orm_answers
    assert sql_answers[1][1][0] == ("Percent international", "N/A")    # nothing to divide by


# --------------------------------------------------------------------------- #
#                         The three command-line tools                        #
# --------------------------------------------------------------------------- #

def test_query_data_prints_all_eleven_questions(sample_data, capsys):
    assert query_data.main() == 0

    printed = capsys.readouterr().out
    assert printed.startswith("Grad Café analysis (raw SQL via psycopg)")
    assert printed.count("\nQuestion ") == 11
    assert "Acceptance %" in printed                     # Question 10's table was printed


@pytest.mark.parametrize(
    ("argv", "question_count"),
    [([], 6), (["--all"], 11)],                          # 1, 4, 5, 8, 9, 10 by default
)
def test_orm_queries_prints_the_requested_questions(sample_data, capsys, argv, question_count):
    assert orm_queries.main(argv) == 0

    printed = capsys.readouterr().out
    assert printed.count("\nQuestion ") == question_count
    assert "Acceptance %" in printed


@pytest.fixture
def report_path(tmp_path, monkeypatch):
    """Where build_query_results writes its HTML during a test (never src/)."""
    path = tmp_path / "query_results.html"
    monkeypatch.setattr(build_query_results, "OUTPUT", path)
    return path


def test_build_query_results_writes_the_report(sample_data, report_path, capsys):
    assert build_query_results.main() == 0

    report = report_path.read_text(encoding="utf-8")
    assert report.count("<section>") == 11
    assert "<strong>240</strong>" in report              # Question 1's answer, in bold
    assert "300 entries added between January 15, 2026 and September 15, 2026" in report
    assert "Wrote query_results.html (11 questions)" in capsys.readouterr().out


def test_build_query_results_handles_an_empty_database(db_conn, report_path):
    assert build_query_results.main() == 0

    report = report_path.read_text(encoding="utf-8")
    assert "0 entries with no dates recorded" in report
    assert "would give N/A" in report


PSYCOPG_TOOLS = [query_data.main, build_query_results.main]
TOOL_NAMES = ["query_data", "build_query_results"]


@pytest.mark.parametrize("tool", PSYCOPG_TOOLS, ids=TOOL_NAMES)
def test_unparseable_settings_exit_2_without_echoing_them(tool, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "dbname='unterminated")

    assert tool() == 2
    printed = capsys.readouterr().err
    assert "connection settings are not valid" in printed
    assert "unterminated" not in printed


@pytest.mark.parametrize("tool", PSYCOPG_TOOLS, ids=TOOL_NAMES)
def test_unreachable_database_exits_2(tool, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://gradcafe@127.0.0.1:1/gradcafe_test")   # nothing listens on port 1

    assert tool() == 2
    assert "cannot connect to PostgreSQL at gradcafe@127.0.0.1:1/gradcafe_test" in capsys.readouterr().err


@pytest.mark.parametrize("tool", PSYCOPG_TOOLS, ids=TOOL_NAMES)
def test_missing_table_exits_3(tool, db_conn, report_path, capsys):
    db_conn.execute("DROP TABLE applicants")             # the next test's db_conn recreates it

    assert tool() == 3
    assert "query failed" in capsys.readouterr().err
    assert not report_path.exists()                      # no half-finished report


def test_orm_queries_unreachable_database_exits_2(monkeypatch, capsys):
    # The ORM's engine is built once at import time, so swap the session factory instead of DATABASE_URL.
    unreachable = create_engine("postgresql+psycopg://gradcafe@127.0.0.1:1/gradcafe_test")
    monkeypatch.setattr(orm_queries, "SessionLocal", sessionmaker(bind=unreachable))

    assert orm_queries.main([]) == 2
    assert "cannot connect to PostgreSQL" in capsys.readouterr().err


def test_orm_queries_missing_table_exits_3(db_conn, capsys):
    db_conn.execute("DROP TABLE applicants")

    assert orm_queries.main([]) == 3
    assert "query failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        ("query_data", ["query_data.py"]),
        ("build_query_results", ["build_query_results.py"]),
        ("orm_queries", ["orm_queries.py", "--no-such-option"]),     # argparse rejects it with code 2
    ],
)
def test_running_each_tool_as_a_script_exits_2_on_bad_settings(module, argv, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "dbname='unterminated")
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module(module, run_name="__main__")

    assert stopped.value.code == 2