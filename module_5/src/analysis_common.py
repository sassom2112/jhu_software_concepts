"""
analysis_common.py - Definitions shared by the raw-SQL and SQLAlchemy analyses.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

query_data.py (handwritten SQL) and orm_queries.py (SQLAlchemy) must answer
the same questions in the same way, and the console, the PDF and the Flask page
must format results identically.  Everything they have to agree on lives here:
the question wording, the matching rules, the valid score ranges, the
number formatting and the plain-text console layout.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

# --------------------------------------------------------------------------- #
#                               Matching rules                                #
# --------------------------------------------------------------------------- #
# Text comparisons ignore case and surrounding spaces.

FALL_2026 = "fall 2026"
FALL_2025 = "fall 2025"

INTERNATIONAL = "international"
AMERICAN = "american"
# A nationality classification is "usable" when it is one of the site's three
# answers; missing or blank values are excluded from the denominator.
NATIONALITY_CLASSES = ("international", "american", "other")

# ILIKE pattern applied to TRIM(status): "Accepted" (and e.g. "accepted", "Acceptance").
ACCEPTED_PATTERN = "accept%"

# PostgreSQL regular expressions (Advanced RE syntax; \m and \M are the start
# and end of a word).  Used with ~* (case-insensitive) unless noted.
JHU_REGEX = r"(johns?\s+hopkins|\mjhu\M)"
COMPUTER_SCIENCE_REGEX = r"(computer\s+science|\meecs\M)"
MASTERS_REGEX = r"^\s*master"
PHD_REGEX = r"^\s*ph\.?\s*d"

# Questions 8 and 9: the four universities, one case-insensitive pattern per school,
# plus the acronym MIT matched case-sensitively so words like "mit" do not count.
# Question 8 applies them to the original `program` text and Question 9 to the
# LLM-standardized university, which is sometimes an acronym or a short form
# ("MIT", "Stanford") rather than the full name, so an exact-name list would miss it.
TARGET_UNIVERSITY_REGEXES = (
    r"georgetown",
    r"massachusetts\s+institute\s+of\s+technology",
    r"stanford",
    r"(carnegie\s+mellon|\mcmu\M)",
)
MIT_ACRONYM_REGEX = r"\mMIT\M"  # used with ~ (case-sensitive)

# A test score or GPA counts as "provided" only when it lies on the official
# scale.  Applicants often type a 260-340 GRE total into the Quantitative
# field, or 99.99 into Analytical Writing; averaging those with real section
# scores would be meaningless.  The stored values are never modified.
GPA_MIN_EXCLUSIVE = 0.0
GPA_MAX = 4.0
GRE_SECTION_MIN = 130.0
GRE_SECTION_MAX = 170.0
GRE_AW_MIN = 0.0
GRE_AW_MAX = 6.0

# Original questions
MIN_ENTRIES_PER_DEGREE = 100
TOP_UNIVERSITY_COUNT = 10

# --------------------------------------------------------------------------- #
#                               Question wording                              #
# --------------------------------------------------------------------------- #

QUESTIONS = {
    "1": "How many entries in the database are from applicants who applied for Fall 2026?",
    "2": "Among entries that provide a nationality classification, what percentage are "
         "international students?",
    "3": "What are the average GPA, GRE Quantitative, GRE Verbal, and GRE Analytical Writing "
         "scores of applicants who provide each metric?",
    "4": "What is the average GPA of American applicants who applied for Fall 2026?",
    "5": "What percentage of Fall 2025 entries are acceptances?",
    "6": "What is the average GPA of accepted applicants who applied for Fall 2026?",
    "7": "How many entries are from applicants who applied to Johns Hopkins University for a "
         "master's degree in Computer Science?",
    "8": "How many Fall 2026 entries are acceptances from applicants applying for a PhD in "
         "Computer Science at Georgetown University, MIT, Stanford University, or Carnegie Mellon "
         "University (using the original downloaded fields)?",
    "9": "Repeating Question 8 with the LLM-generated program and university fields, how does the "
         "count compare with the original-field count?",
    "10": "Original question: For Fall 2026, how do the acceptance rate and the average GPA of "
          "accepted applicants compare across degree types that have at least 100 entries?",
    "11": "Original question: Which ten universities (LLM-standardized names) have the most "
          "Fall 2026 entries, and what percentage of each university's entries report an "
          "acceptance?",
}

# --------------------------------------------------------------------------- #
#                                  Formatting                                 #
# --------------------------------------------------------------------------- #

NOT_AVAILABLE = "N/A"


def _two_places(value: object) -> str:
    """Round half-up to two decimals (the same rule as PostgreSQL's ROUND on numeric)."""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_count(value: object) -> str:
    """Whole number with thousands separators: 30066 -> '30,066'."""
    if value is None:
        return "0"
    return f"{int(value):,}"


def format_percent(value: object) -> str:
    """Two decimals and a percent sign: 46.353 -> '46.35%'."""
    if value is None:
        return NOT_AVAILABLE
    return f"{_two_places(value)}%"


def format_average(value: object) -> str:
    """Two decimals: 3.7712 -> '3.77'."""
    if value is None:
        return NOT_AVAILABLE
    return _two_places(value)


def format_average_with_count(value: object, count: object) -> str:
    """An average and how many values it is based on: '3.77 (n = 18,287)'."""
    return f"{format_average(value)} (n = {format_count(count)})"


def format_difference(value: object) -> str:
    """Signed whole number: 3 -> '+3', -2 -> '-2', 0 -> '0'."""
    number = int(value or 0)
    return f"{number:+d}" if number else "0"


def format_table(columns: list[str], rows: list[list[str]]) -> list[str]:
    """Plain-text table lines: first column left-aligned, the others right-aligned."""
    widths = [
        max(len(str(value)) for value in [column] + [row[i] for row in rows])
        for i, column in enumerate(columns)
    ]

    def line(values: list[str]) -> str:
        cells = [
            str(v).ljust(widths[0]) if i == 0 else str(v).rjust(widths[i])
            for i, v in enumerate(values)
        ]
        return "  ".join(cells)

    return [line(columns), "  ".join("-" * width for width in widths)] + [line(row) for row in rows]


# --------------------------------------------------------------------------- #
#                                   Console                                   #
# --------------------------------------------------------------------------- #


def print_answers(answers: Iterable[Any], title: str) -> None:
    """Print answers as plain text under a title (the console output of both analyses).

    Each answer is a query_data.Answer or an orm_queries.OrmAnswer: anything with
    ``number``, ``question``, ``lines``, ``columns`` and ``table`` attributes.
    """
    print(title)
    print("=" * len(title))
    for answer in answers:
        print(f"\nQuestion {answer.number}: {answer.question}")
        for label, value in answer.lines:
            print(f"  {label}: {value}")
        if answer.table:
            for text in format_table(answer.columns, answer.table):
                print(f"  {text}")
