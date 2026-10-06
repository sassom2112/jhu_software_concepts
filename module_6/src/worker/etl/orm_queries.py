"""
orm_queries.py - Answer the Module 3 questions with the SQLAlchemy ORM.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

Every query here is built from the Applicant model with select(), where(),
func.count(), func.avg(), and_() / or_() and a Session.  No handwritten SQL
strings and no database-driver calls appear in this file.  The matching rules, valid score
ranges and formatting come from analysis_common.py, the same definitions
query_data.py uses, so equivalent questions give identical answers.

Console usage::

    python -m worker.etl.orm_queries          # Questions 1, 4, 5, 8, 9 and original Question 10
    python -m worker.etl.orm_queries --all    # all eleven questions (what the web page shows)
"""

# SQLAlchemy generates func.count() & co. at runtime, so Pylint wrongly reports "not callable".
# pylint: disable=not-callable

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import Numeric, and_, cast, func, literal, or_, select
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from db.db_config import describe_target
from db.query_limits import MAX_LIMIT
from worker.etl import analysis_common as rules
from worker.etl.analysis_common import (QUESTIONS, format_average, format_average_with_count,
                                        format_count, format_difference, format_percent,
                                        print_answers)
from worker.etl.models import Applicant, SessionLocal

ONE_ROW = 1  # every aggregate below returns a single row; LIMIT says so explicitly
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
    """The same four universities in the LLM-standardized name.

    That name may be an acronym such as 'MIT'.
    """
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
    stmt = (select(func.count()).select_from(Applicant).where(_is_term(rules.FALL_2026))
            .limit(ONE_ROW))
    return session.scalar(stmt) or 0


def q2_percent_international(session: Session) -> tuple[int, int, Decimal | None]:
    """Question 2: (international, classified, percent international).

    Only entries that provide a nationality classification are counted.
    """
    nationality = _normalized(Applicant.us_or_international)
    classified = func.count()
    international = func.count().filter(nationality == rules.INTERNATIONAL)
    stmt = (
        select(international, classified, _rounded_percent(international, classified))
        .where(nationality.in_(rules.NATIONALITY_CLASSES))
        .limit(ONE_ROW)
    )
    return tuple(session.execute(stmt).one())


def q3_average_scores(session: Session) -> dict[str, tuple[Decimal | None, int]]:
    """Question 3: GPA and the three GRE scores.

    Returns {metric: (average on the official scale, number of values)}.
    """
    gpa, quant, verbal, writing = _valid_gpa(), _valid_section_score(Applicant.gre), \
        _valid_section_score(Applicant.gre_v), _valid_writing_score()
    stmt = select(
        _rounded_average(Applicant.gpa, gpa), func.count(Applicant.gpa).filter(gpa),
        _rounded_average(Applicant.gre, quant), func.count(Applicant.gre).filter(quant),
        _rounded_average(Applicant.gre_v, verbal), func.count(Applicant.gre_v).filter(verbal),
        _rounded_average(Applicant.gre_aw, writing), func.count(Applicant.gre_aw).filter(writing),
    ).limit(ONE_ROW)
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
    ).limit(ONE_ROW)
    return tuple(session.execute(stmt).one())


def q5_fall_2025_acceptance(session: Session) -> tuple[int, int, Decimal | None]:
    """Question 5: (accepted, all, acceptance percent) for Fall 2025 entries."""
    total = func.count()
    accepted = func.count().filter(_is_accepted())
    stmt = (
        select(accepted, total, _rounded_percent(accepted, total))
        .where(_is_term(rules.FALL_2025))
        .limit(ONE_ROW)
    )
    return tuple(session.execute(stmt).one())


def q6_accepted_fall_2026_gpa(session: Session) -> tuple[Decimal | None, int]:
    """Question 6: (average GPA, number of GPAs) of accepted Fall 2026 applicants."""
    stmt = select(_rounded_average(Applicant.gpa), func.count(Applicant.gpa)).where(
        and_(_is_term(rules.FALL_2026), _is_accepted(), _valid_gpa())
    ).limit(ONE_ROW)
    return tuple(session.execute(stmt).one())


def q7_jhu_cs_masters(session: Session) -> int:
    """Question 7: the number of Johns Hopkins Computer Science master's entries."""
    stmt = select(func.count()).select_from(Applicant).where(
        and_(
            Applicant.program.op("~*")(rules.JHU_REGEX),
            _mentions_computer_science(Applicant.program),
            _is_masters(),
        )
    ).limit(ONE_ROW)
    return session.scalar(stmt) or 0


def _q8_base_conditions():
    return and_(_is_term(rules.FALL_2026), _is_accepted(), _is_phd())


def q8_accepted_cs_phd_original(session: Session) -> int:
    """Question 8: accepted Fall 2026 CS PhD entries at the four schools (original fields)."""
    stmt = select(func.count()).select_from(Applicant).where(
        and_(
            _q8_base_conditions(),
            _mentions_computer_science(Applicant.program),
            _original_target_university(),
        )
    ).limit(ONE_ROW)
    return session.scalar(stmt) or 0


def q9_accepted_cs_phd_llm(session: Session) -> tuple[int, int]:
    """Question 9: (original-field count, LLM-field count) for the Question 8 selection."""
    original = func.count().filter(
        and_(_mentions_computer_science(Applicant.program), _original_target_university())
    )
    llm = func.count().filter(
        and_(_mentions_computer_science(Applicant.llm_generated_program), _llm_target_university())
    )
    stmt = select(original, llm).where(_q8_base_conditions()).limit(ONE_ROW)
    return tuple(session.execute(stmt).one())


def q10_degree_comparison(session: Session) -> list[tuple]:
    """Question 10: acceptance rate and accepted GPA per degree with 100+ Fall 2026 entries.

    Returns (degree, entries, acceptances, percent, average accepted GPA) rows.
    """
    entries = func.count()
    acceptances = func.count().filter(_is_accepted())
    accepted_gpa = _rounded_average(Applicant.gpa, and_(_is_accepted(), _valid_gpa()))
    stmt = (
        select(
            Applicant.degree,
            entries.label("entries"),
            acceptances.label("acceptances"),
            _rounded_percent(acceptances, entries).label("acceptance_percent"),
            accepted_gpa.label("avg_accepted_gpa"),
        )
        .where(_is_term(rules.FALL_2026), Applicant.degree.is_not(None))
        .group_by(Applicant.degree)
        .having(func.count() >= rules.MIN_ENTRIES_PER_DEGREE)
        .order_by(func.count().desc(), Applicant.degree)
        .limit(MAX_LIMIT)
    )
    return [tuple(row) for row in session.execute(stmt).all()]


def q11_top_universities(session: Session) -> list[tuple]:
    """Question 11: the ten LLM-named schools with the most Fall 2026 entries.

    Returns (university, entries, acceptances, percent accepted) rows.
    """
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
    stmt = select(func.count(), func.max(Applicant.date_added)).limit(ONE_ROW)
    total, newest = session.execute(stmt).one()
    return {"total_entries": total, "newest_entry": newest}


# --------------------------------------------------------------------------- #
#                         Answers ready for display                           #
# --------------------------------------------------------------------------- #


def _answer(number: str, lines=None, columns=None, table=None) -> OrmAnswer:
    """An OrmAnswer for question *number*, worded as in analysis_common.QUESTIONS."""
    return OrmAnswer(number, QUESTIONS[number], lines or [], columns or [], table or [])


# One builder per question: run the query and format its result for display.

def _answer_q1(session: Session) -> OrmAnswer:
    return _answer("1", [
        ("Fall 2026 applicant count", format_count(q1_fall_2026_count(session))),
    ])


def _answer_q2(session: Session) -> OrmAnswer:
    international, classified, percent = q2_percent_international(session)
    return _answer("2", [
        ("Percent international", format_percent(percent)),
        ("International entries", format_count(international)),
        ("Entries with a nationality classification", format_count(classified)),
    ])


def _answer_q3(session: Session) -> OrmAnswer:
    labels = {"gpa": "Average GPA", "gre": "Average GRE Quantitative",
              "gre_v": "Average GRE Verbal", "gre_aw": "Average GRE Analytical Writing"}
    scores = q3_average_scores(session)
    return _answer("3", [(labels[metric], format_average_with_count(average, n))
                         for metric, (average, n) in scores.items()])


def _answer_q4(session: Session) -> OrmAnswer:
    average, n = q4_american_fall_2026_gpa(session)
    return _answer("4", [
        ("Average GPA of American Fall 2026 applicants", format_average(average)),
        ("Applicants with a GPA", format_count(n)),
    ])


def _answer_q5(session: Session) -> OrmAnswer:
    accepted, total, percent = q5_fall_2025_acceptance(session)
    return _answer("5", [
        ("Fall 2025 acceptance percentage", format_percent(percent)),
        ("Accepted Fall 2025 entries", format_count(accepted)),
        ("All Fall 2025 entries", format_count(total)),
    ])


def _answer_q6(session: Session) -> OrmAnswer:
    average, n = q6_accepted_fall_2026_gpa(session)
    return _answer("6", [
        ("Average GPA of accepted Fall 2026 applicants", format_average(average)),
        ("Applicants with a GPA", format_count(n)),
    ])


def _answer_q7(session: Session) -> OrmAnswer:
    return _answer("7", [
        ("JHU Computer Science master's entries", format_count(q7_jhu_cs_masters(session))),
    ])


def _answer_q8(session: Session) -> OrmAnswer:
    return _answer("8", [
        ("Accepted Fall 2026 CS PhD entries (original fields)",
         format_count(q8_accepted_cs_phd_original(session))),
    ])


def _answer_q9(session: Session) -> OrmAnswer:
    original, llm = q9_accepted_cs_phd_llm(session)
    return _answer("9", [
        ("Original-field count", format_count(original)),
        ("LLM-field count", format_count(llm)),
        ("Difference", format_difference(llm - original)),
    ])


def _answer_q10(session: Session) -> OrmAnswer:
    columns = ["Degree", "Entries", "Acceptances", "Acceptance %", "Avg GPA (accepted)"]
    table = [
        [degree, format_count(entries), format_count(accepted), format_percent(percent),
         format_average(gpa)]
        for degree, entries, accepted, percent, gpa in q10_degree_comparison(session)
    ]
    return _answer("10", columns=columns, table=table)


def _answer_q11(session: Session) -> OrmAnswer:
    columns = ["University", "Entries", "Acceptances", "Acceptance %"]
    table = [
        [university, format_count(entries), format_count(accepted), format_percent(percent)]
        for university, entries, accepted, percent in q11_top_universities(session)
    ]
    return _answer("11", columns=columns, table=table)


# In question order: build_answers keeps this order whatever order it is asked in.
ANSWER_BUILDERS = {
    "1": _answer_q1, "2": _answer_q2, "3": _answer_q3, "4": _answer_q4,
    "5": _answer_q5, "6": _answer_q6, "7": _answer_q7, "8": _answer_q8,
    "9": _answer_q9, "10": _answer_q10, "11": _answer_q11,
}


def build_answers(session: Session, numbers: tuple[str, ...] | None = None) -> list[OrmAnswer]:
    """Compute and format the requested questions (all eleven by default)."""
    wanted = set(numbers or QUESTIONS.keys())
    return [build(session) for number, build in ANSWER_BUILDERS.items() if number in wanted]


def get_analysis() -> dict:
    """Everything the web page displays, computed through the ORM in one session.

    The page itself shows the SQL snapshot (analytics.py); the tests check the two agree."""
    with SessionLocal() as session:
        return {"summary": database_summary(session), "answers": build_answers(session)}


# --------------------------------------------------------------------------- #
#                                   Console                                   #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """Console entry point: print the required ORM questions (all eleven with ``--all``).

    Returns the exit code: 0, 2 (cannot connect) or 3 (query failed).
    """
    parser = argparse.ArgumentParser(description="Grad Café analysis with the SQLAlchemy ORM")
    parser.add_argument("--all", action="store_true", help="print all eleven questions")
    args = parser.parse_args(argv)
    numbers = None if args.all else REQUIRED_ORM_QUESTIONS
    try:
        with SessionLocal() as session:
            answers = build_answers(session, numbers)
    except OperationalError as err:
        print(f"error: cannot connect to PostgreSQL at {describe_target()}: {err.orig}",
              file=sys.stderr)
        return 2
    except SQLAlchemyError as err:
        print(f"error: query failed: {err}", file=sys.stderr)
        return 3
    print_answers(answers, "Grad Café analysis (SQLAlchemy ORM)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
