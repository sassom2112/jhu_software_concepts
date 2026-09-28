"""
orm_queries.py - Answer the Module 3 questions with the SQLAlchemy ORM.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

Every query here is built from the Applicant model with select(), where(),
func.count(), func.avg(), and_() / or_() and a Session.  No handwritten SQL
strings and no database-driver calls appear in this file.  The matching rules, valid score
ranges and formatting come from analysis_common.py, the same definitions
query_data.py uses, so equivalent questions give identical answers.

Console usage::

    python orm_queries.py          # Questions 1, 4, 5, 8, 9 and original Question 10
    python orm_queries.py --all    # all eleven questions (what the Flask page shows)
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import Numeric, and_, cast, func, literal, or_, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

import analysis_common as rules
from analysis_common import (QUESTIONS, format_average, format_count, format_difference, format_percent,
                             format_table)
from db_config import describe_target
from models import Applicant, SessionLocal

REQUIRED_ORM_QUESTIONS = ("1", "4", "5", "8", "9", "10")


@dataclass
class OrmAnswer:
    """One analysis result computed through the ORM."""

    number: str
    question: str
    lines: list[tuple[str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    table: list[list[str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
#                         Reusable column expressions                         #
# --------------------------------------------------------------------------- #


def _normalized(column):
    """LOWER(TRIM(column)) for case- and space-insensitive comparisons."""
    return func.lower(func.trim(column))


def _is_term(term: str):
    return _normalized(Applicant.term) == term


def _is_accepted():
    return func.trim(Applicant.status).ilike(rules.ACCEPTED_PATTERN)


def _is_phd():
    return Applicant.degree.op("~*")(rules.PHD_REGEX)


def _is_masters():
    return Applicant.degree.op("~*")(rules.MASTERS_REGEX)


def _mentions_computer_science(column):
    return column.op("~*")(rules.COMPUTER_SCIENCE_REGEX)


def _target_university(column):
    """One of the four Question 8/9 universities in *column*: its name, or the MIT / CMU acronym."""
    patterns = [column.op("~*")(regex) for regex in rules.TARGET_UNIVERSITY_REGEXES]
    patterns.append(column.op("~")(rules.MIT_ACRONYM_REGEX))
    return or_(*patterns)


def _original_target_university():
    """One of the four Question 8 universities, recognized in the original program text."""
    return _target_university(Applicant.program)


def _llm_target_university():
    """The same four universities in the LLM-standardized name, which may be an acronym such as 'MIT'."""
    return _target_university(Applicant.llm_generated_university)


def _valid_gpa():
    return and_(Applicant.gpa > rules.GPA_MIN_EXCLUSIVE, Applicant.gpa <= rules.GPA_MAX)


def _valid_section_score(column):
    return column.between(rules.GRE_SECTION_MIN, rules.GRE_SECTION_MAX)


def _valid_writing_score():
    return Applicant.gre_aw.between(rules.GRE_AW_MIN, rules.GRE_AW_MAX)


def _rounded_average(column, condition=None):
    """ROUND(AVG(column)::numeric, 2), optionally with FILTER (WHERE condition)."""
    average = func.avg(column)
    if condition is not None:
        average = average.filter(condition)
    return func.round(cast(average, Numeric), 2)


def _rounded_percent(part, whole):
    """ROUND(100.0 * part / NULLIF(whole, 0), 2) with numeric (not floating point) arithmetic."""
    return func.round(literal(Decimal("100.0"), Numeric) * part / func.nullif(whole, 0), 2)


# --------------------------------------------------------------------------- #
#                                  Questions                                  #
# --------------------------------------------------------------------------- #


def q1_fall_2026_count(session: Session) -> int:
    """Question 1: the number of Fall 2026 entries."""
    stmt = select(func.count()).select_from(Applicant).where(_is_term(rules.FALL_2026))
    return session.scalar(stmt) or 0


def q2_percent_international(session: Session) -> tuple[int, int, Decimal | None]:
    """Question 2: (international, classified, percent international) among entries with a nationality."""
    classified = func.count()
    international = func.count().filter(_normalized(Applicant.us_or_international) == rules.INTERNATIONAL)
    stmt = (
        select(international, classified, _rounded_percent(international, classified))
        .where(_normalized(Applicant.us_or_international).in_(rules.NATIONALITY_CLASSES))
    )
    return tuple(session.execute(stmt).one())


def q3_average_scores(session: Session) -> dict[str, tuple[Decimal | None, int]]:
    """Question 3: {metric: (average on the official scale, number of values)} for GPA and the GRE scores."""
    gpa, quant, verbal, writing = _valid_gpa(), _valid_section_score(Applicant.gre), \
        _valid_section_score(Applicant.gre_v), _valid_writing_score()
    stmt = select(
        _rounded_average(Applicant.gpa, gpa), func.count(Applicant.gpa).filter(gpa),
        _rounded_average(Applicant.gre, quant), func.count(Applicant.gre).filter(quant),
        _rounded_average(Applicant.gre_v, verbal), func.count(Applicant.gre_v).filter(verbal),
        _rounded_average(Applicant.gre_aw, writing), func.count(Applicant.gre_aw).filter(writing),
    )
    r = session.execute(stmt).one()
    return {"gpa": (r[0], r[1]), "gre": (r[2], r[3]), "gre_v": (r[4], r[5]), "gre_aw": (r[6], r[7])}


def q4_american_fall_2026_gpa(session: Session) -> tuple[Decimal | None, int]:
    """Question 4: (average GPA, number of GPAs) of American Fall 2026 applicants."""
    stmt = select(_rounded_average(Applicant.gpa), func.count(Applicant.gpa)).where(
        and_(
            _is_term(rules.FALL_2026),
            _normalized(Applicant.us_or_international) == rules.AMERICAN,
            _valid_gpa(),
        )
    )
    return tuple(session.execute(stmt).one())


def q5_fall_2025_acceptance(session: Session) -> tuple[int, int, Decimal | None]:
    """Question 5: (accepted, all, acceptance percent) for Fall 2025 entries."""
    total = func.count()
    accepted = func.count().filter(_is_accepted())
    stmt = select(accepted, total, _rounded_percent(accepted, total)).where(_is_term(rules.FALL_2025))
    return tuple(session.execute(stmt).one())


def q6_accepted_fall_2026_gpa(session: Session) -> tuple[Decimal | None, int]:
    """Question 6: (average GPA, number of GPAs) of accepted Fall 2026 applicants."""
    stmt = select(_rounded_average(Applicant.gpa), func.count(Applicant.gpa)).where(
        and_(_is_term(rules.FALL_2026), _is_accepted(), _valid_gpa())
    )
    return tuple(session.execute(stmt).one())


def q7_jhu_cs_masters(session: Session) -> int:
    """Question 7: the number of Johns Hopkins Computer Science master's entries."""
    stmt = select(func.count()).select_from(Applicant).where(
        and_(
            Applicant.program.op("~*")(rules.JHU_REGEX),
            _mentions_computer_science(Applicant.program),
            _is_masters(),
        )
    )
    return session.scalar(stmt) or 0


def _q8_base_conditions():
    return and_(_is_term(rules.FALL_2026), _is_accepted(), _is_phd())


def q8_accepted_cs_phd_original(session: Session) -> int:
    """Question 8: accepted Fall 2026 CS PhD entries at the four schools, from the original fields."""
    stmt = select(func.count()).select_from(Applicant).where(
        and_(_q8_base_conditions(), _mentions_computer_science(Applicant.program), _original_target_university())
    )
    return session.scalar(stmt) or 0


def q9_accepted_cs_phd_llm(session: Session) -> tuple[int, int]:
    """Question 9: (original-field count, LLM-field count) for the Question 8 selection."""
    original = func.count().filter(and_(_mentions_computer_science(Applicant.program), _original_target_university()))
    llm = func.count().filter(and_(_mentions_computer_science(Applicant.llm_generated_program), _llm_target_university()))
    stmt = select(original, llm).where(_q8_base_conditions())
    return tuple(session.execute(stmt).one())


def q10_degree_comparison(session: Session) -> list[tuple]:
    """Question 10: (degree, entries, acceptances, percent, average accepted GPA) per degree with 100+ Fall 2026 entries."""
    entries = func.count()
    acceptances = func.count().filter(_is_accepted())
    stmt = (
        select(
            Applicant.degree,
            entries.label("entries"),
            acceptances.label("acceptances"),
            _rounded_percent(acceptances, entries).label("acceptance_percent"),
            _rounded_average(Applicant.gpa, and_(_is_accepted(), _valid_gpa())).label("avg_accepted_gpa"),
        )
        .where(_is_term(rules.FALL_2026), Applicant.degree.is_not(None))
        .group_by(Applicant.degree)
        .having(func.count() >= rules.MIN_ENTRIES_PER_DEGREE)
        .order_by(func.count().desc(), Applicant.degree)
    )
    return [tuple(row) for row in session.execute(stmt).all()]


def q11_top_universities(session: Session) -> list[tuple]:
    """Question 11: (university, entries, acceptances, percent) for the ten LLM-named schools with the most Fall 2026 entries."""
    entries = func.count()
    acceptances = func.count().filter(_is_accepted())
    stmt = (
        select(
            Applicant.llm_generated_university,
            entries.label("entries"),
            acceptances.label("acceptances"),
            _rounded_percent(acceptances, entries).label("acceptance_percent"),
        )
        .where(
            _is_term(rules.FALL_2026),
            Applicant.llm_generated_university.is_not(None),
            Applicant.llm_generated_university != "Unknown",
        )
        .group_by(Applicant.llm_generated_university)
        .order_by(func.count().desc(), Applicant.llm_generated_university)
        .limit(rules.TOP_UNIVERSITY_COUNT)
    )
    return [tuple(row) for row in session.execute(stmt).all()]


def database_summary(session: Session) -> dict:
    """Row count and newest entry date, shown at the top of the web page."""
    total, newest = session.execute(select(func.count(), func.max(Applicant.date_added))).one()
    return {"total_entries": total, "newest_entry": newest}


# --------------------------------------------------------------------------- #
#                         Answers ready for display                           #
# --------------------------------------------------------------------------- #


def build_answers(session: Session, numbers: tuple[str, ...] | None = None) -> list[OrmAnswer]:
    """Compute and format the requested questions (all eleven by default)."""
    wanted = set(numbers or QUESTIONS.keys())
    answers: list[OrmAnswer] = []

    def add(number: str, lines=None, columns=None, table=None):
        answers.append(OrmAnswer(number, QUESTIONS[number], lines or [], columns or [], table or []))

    if "1" in wanted:
        add("1", [("Fall 2026 applicant count", format_count(q1_fall_2026_count(session)))])
    if "2" in wanted:
        international, classified, percent = q2_percent_international(session)
        add("2", [("Percent international", format_percent(percent)),
                  ("International entries", format_count(international)),
                  ("Entries with a nationality classification", format_count(classified))])
    if "3" in wanted:
        scores = q3_average_scores(session)
        labels = {"gpa": "Average GPA", "gre": "Average GRE Quantitative",
                  "gre_v": "Average GRE Verbal", "gre_aw": "Average GRE Analytical Writing"}
        add("3", [(labels[k], f"{format_average(v)} (n = {format_count(n)})") for k, (v, n) in scores.items()])
    if "4" in wanted:
        average, n = q4_american_fall_2026_gpa(session)
        add("4", [("Average GPA of American Fall 2026 applicants", format_average(average)),
                  ("Applicants with a GPA", format_count(n))])
    if "5" in wanted:
        accepted, total, percent = q5_fall_2025_acceptance(session)
        add("5", [("Fall 2025 acceptance percentage", format_percent(percent)),
                  ("Accepted Fall 2025 entries", format_count(accepted)),
                  ("All Fall 2025 entries", format_count(total))])
    if "6" in wanted:
        average, n = q6_accepted_fall_2026_gpa(session)
        add("6", [("Average GPA of accepted Fall 2026 applicants", format_average(average)),
                  ("Applicants with a GPA", format_count(n))])
    if "7" in wanted:
        add("7", [("JHU Computer Science master's entries", format_count(q7_jhu_cs_masters(session)))])
    if "8" in wanted:
        add("8", [("Accepted Fall 2026 CS PhD entries (original fields)",
                   format_count(q8_accepted_cs_phd_original(session)))])
    if "9" in wanted:
        original, llm = q9_accepted_cs_phd_llm(session)
        add("9", [("Original-field count", format_count(original)), ("LLM-field count", format_count(llm)),
                  ("Difference", format_difference(llm - original))])
    if "10" in wanted:
        rows = q10_degree_comparison(session)
        add("10", columns=["Degree", "Entries", "Acceptances", "Acceptance %", "Avg GPA (accepted)"],
            table=[[d, format_count(e), format_count(a), format_percent(p), format_average(g)] for d, e, a, p, g in rows])
    if "11" in wanted:
        rows = q11_top_universities(session)
        add("11", columns=["University", "Entries", "Acceptances", "Acceptance %"],
            table=[[u, format_count(e), format_count(a), format_percent(p)] for u, e, a, p in rows])
    return answers


def get_analysis() -> dict:
    """Everything the Flask page displays, read through the ORM in one session."""
    with SessionLocal() as session:
        return {"summary": database_summary(session), "answers": build_answers(session)}


# --------------------------------------------------------------------------- #
#                                   Console                                   #
# --------------------------------------------------------------------------- #


def _print(answers: list[OrmAnswer], title: str) -> None:
    print(title)
    print("=" * len(title))
    for answer in answers:
        print(f"\nQuestion {answer.number}: {answer.question}")
        for label, value in answer.lines:
            print(f"  {label}: {value}")
        if answer.table:
            for table_line in format_table(answer.columns, answer.table):
                print(f"  {table_line}")


def main(argv: list[str] | None = None) -> int:
    """Console entry point: print the required ORM questions (all eleven with ``--all``); returns the exit code."""
    parser = argparse.ArgumentParser(description="Grad Café analysis with the SQLAlchemy ORM")
    parser.add_argument("--all", action="store_true", help="print all eleven questions")
    args = parser.parse_args(argv)
    numbers = None if args.all else REQUIRED_ORM_QUESTIONS
    try:
        with SessionLocal() as session:
            answers = build_answers(session, numbers)
    except OperationalError as err:
        print(f"error: cannot connect to PostgreSQL at {describe_target()}: {err.orig}", file=sys.stderr)
        return 2
    except SQLAlchemyError as err:
        print(f"error: query failed: {err}", file=sys.stderr)
        return 3
    _print(answers, "Grad Café analysis (SQLAlchemy ORM)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
