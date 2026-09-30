"""
query_data.py - Answer the Module 3 questions with handwritten SQL (psycopg 3).

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

Every analysis below is expressed entirely in SQL; Python only sends the query
and formats the numbers it gets back.  The same SQL text, question wording and
explanations are reused by build_query_results.py to produce
query_results.pdf, so the PDF always shows exactly what this file runs.

Usage::

    python query_data.py

Connection settings come from the environment (see db_config.py).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from analysis_common import (ACCEPTED_PATTERN, QUESTIONS, TOP_UNIVERSITY_COUNT, format_average,
                             format_average_with_count, format_count, format_difference,
                             format_percent, print_answers)
from db_config import TABLE_NAME, run_with_connection
from query_limits import MAX_LIMIT, clamp_limit


@dataclass
class Answer:
    """One analysis result, ready for the console, the PDF or a web page."""

    number: str
    question: str
    sql: str
    explanation: str
    lines: list[tuple[str, str]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    table: list[list[str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
#                                   The SQL                                   #
# --------------------------------------------------------------------------- #
# Each statement is composed once, here, with psycopg's sql module: the table
# name is an sql.Identifier and every value - the "accept%" status pattern and
# the row LIMIT - is a named placeholder bound at execution time.  No f-string,
# + or .format() on raw text ever builds SQL.

# Rows each question may return: one for the single-row answers, the global
# maximum for Question 10's per-degree table, the top ten for Question 11.
ROW_LIMITS = {**{str(n): 1 for n in range(1, 10)}, "10": MAX_LIMIT, "11": TOP_UNIVERSITY_COUNT}


def _statement(text: str) -> sql.Composed:
    """Compose one question's SQL: {table} becomes the quoted table name, {accepted}
    and {limit} become the named placeholders %(accepted)s and %(limit)s."""
    return sql.SQL(text).format(
        table=sql.Identifier(TABLE_NAME),
        accepted=sql.Placeholder("accepted"),
        limit=sql.Placeholder("limit"),
    )


def statement_params(number: str) -> dict[str, object]:
    """The values bound to question *number*'s placeholders (the LIMIT is clamped)."""
    return {"accepted": ACCEPTED_PATTERN, "limit": clamp_limit(ROW_LIMITS[number], default=1)}


SQL_Q1 = _statement(r"""
SELECT COUNT(*) AS fall_2026_entries
FROM {table}
WHERE LOWER(TRIM(term)) = 'fall 2026'
LIMIT {limit}
""")

SQL_Q2 = _statement(r"""
SELECT
    COUNT(*) FILTER (
        WHERE LOWER(TRIM(us_or_international)) = 'international'
    ) AS international_entries,
    COUNT(*) AS classified_entries,
    ROUND(100.0 * COUNT(*) FILTER (
        WHERE LOWER(TRIM(us_or_international)) = 'international'
    ) / NULLIF(COUNT(*), 0), 2) AS percent_international
FROM {table}
WHERE LOWER(TRIM(us_or_international)) IN ('international', 'american', 'other')
LIMIT {limit}
""")

SQL_Q3 = _statement(r"""
SELECT
    ROUND((AVG(gpa)    FILTER (WHERE gpa > 0 AND gpa <= 4.0))::numeric, 2)   AS avg_gpa,
    COUNT(gpa)         FILTER (WHERE gpa > 0 AND gpa <= 4.0)                  AS gpa_n,
    ROUND((AVG(gre)    FILTER (WHERE gre BETWEEN 130 AND 170))::numeric, 2)   AS avg_gre_quant,
    COUNT(gre)         FILTER (WHERE gre BETWEEN 130 AND 170)                 AS gre_quant_n,
    ROUND((AVG(gre_v)  FILTER (WHERE gre_v BETWEEN 130 AND 170))::numeric, 2) AS avg_gre_verbal,
    COUNT(gre_v)       FILTER (WHERE gre_v BETWEEN 130 AND 170)               AS gre_verbal_n,
    ROUND((AVG(gre_aw) FILTER (WHERE gre_aw BETWEEN 0 AND 6))::numeric, 2)    AS avg_gre_aw,
    COUNT(gre_aw)      FILTER (WHERE gre_aw BETWEEN 0 AND 6)                  AS gre_aw_n
FROM {table}
LIMIT {limit}
""")

SQL_Q4 = _statement(r"""
SELECT
    ROUND(AVG(gpa)::numeric, 2) AS avg_gpa,
    COUNT(gpa)                  AS applicants_with_gpa
FROM {table}
WHERE LOWER(TRIM(term)) = 'fall 2026'
  AND LOWER(TRIM(us_or_international)) = 'american'
  AND gpa > 0 AND gpa <= 4.0
LIMIT {limit}
""")

SQL_Q5 = _statement(r"""
SELECT
    COUNT(*) FILTER (WHERE TRIM(status) ILIKE {accepted})              AS accepted_entries,
    COUNT(*)                                                          AS fall_2025_entries,
    ROUND(100.0 * COUNT(*) FILTER (WHERE TRIM(status) ILIKE {accepted})
          / NULLIF(COUNT(*), 0), 2)                                   AS acceptance_percent
FROM {table}
WHERE LOWER(TRIM(term)) = 'fall 2025'
LIMIT {limit}
""")

SQL_Q6 = _statement(r"""
SELECT
    ROUND(AVG(gpa)::numeric, 2) AS avg_gpa,
    COUNT(gpa)                  AS applicants_with_gpa
FROM {table}
WHERE LOWER(TRIM(term)) = 'fall 2026'
  AND TRIM(status) ILIKE {accepted}
  AND gpa > 0 AND gpa <= 4.0
LIMIT {limit}
""")

SQL_Q7 = _statement(r"""
SELECT COUNT(*) AS jhu_cs_masters_entries
FROM {table}
WHERE program ~* '(johns?\s+hopkins|\mjhu\M)'
  AND program ~* '(computer\s+science|\meecs\M)'
  AND degree  ~* '^\s*master'
LIMIT {limit}
""")

SQL_Q8 = _statement(r"""
SELECT COUNT(*) AS accepted_cs_phd_entries
FROM {table}
WHERE LOWER(TRIM(term)) = 'fall 2026'
  AND TRIM(status) ILIKE {accepted}
  AND degree  ~* '^\s*ph\.?\s*d'
  AND program ~* '(computer\s+science|\meecs\M)'
  AND (   program ~* 'georgetown'
       OR program ~* 'massachusetts\s+institute\s+of\s+technology'
       OR program ~  '\mMIT\M'
       OR program ~* 'stanford'
       OR program ~* '(carnegie\s+mellon|\mcmu\M)')
LIMIT {limit}
""")

SQL_Q9 = _statement(r"""
SELECT
    COUNT(*) FILTER (
        WHERE program ~* '(computer\s+science|\meecs\M)'
          AND (   program ~* 'georgetown'
               OR program ~* 'massachusetts\s+institute\s+of\s+technology'
               OR program ~  '\mMIT\M'
               OR program ~* 'stanford'
               OR program ~* '(carnegie\s+mellon|\mcmu\M)')
    ) AS original_field_count,
    COUNT(*) FILTER (
        WHERE llm_generated_program ~* '(computer\s+science|\meecs\M)'
          AND (   llm_generated_university ~* 'georgetown'
               OR llm_generated_university ~* 'massachusetts\s+institute\s+of\s+technology'
               OR llm_generated_university ~  '\mMIT\M'
               OR llm_generated_university ~* 'stanford'
               OR llm_generated_university ~* '(carnegie\s+mellon|\mcmu\M)')
    ) AS llm_field_count
FROM {table}
WHERE LOWER(TRIM(term)) = 'fall 2026'
  AND TRIM(status) ILIKE {accepted}
  AND degree ~* '^\s*ph\.?\s*d'
LIMIT {limit}
""")

SQL_Q10 = _statement(r"""
SELECT
    degree,
    COUNT(*)                                                                AS entries,
    COUNT(*) FILTER (WHERE TRIM(status) ILIKE {accepted})                    AS acceptances,
    ROUND(100.0 * COUNT(*) FILTER (WHERE TRIM(status) ILIKE {accepted})
          / NULLIF(COUNT(*), 0), 2)                                         AS acceptance_percent,
    ROUND((AVG(gpa) FILTER (WHERE TRIM(status) ILIKE {accepted}
                              AND gpa > 0 AND gpa <= 4.0))::numeric, 2)     AS avg_accepted_gpa
FROM {table}
WHERE LOWER(TRIM(term)) = 'fall 2026'
  AND degree IS NOT NULL
GROUP BY degree
HAVING COUNT(*) >= 100
ORDER BY entries DESC, degree
LIMIT {limit}
""")

SQL_Q11 = _statement(r"""
SELECT
    llm_generated_university                                                AS university,
    COUNT(*)                                                                AS entries,
    COUNT(*) FILTER (WHERE TRIM(status) ILIKE {accepted})                    AS acceptances,
    ROUND(100.0 * COUNT(*) FILTER (WHERE TRIM(status) ILIKE {accepted})
          / NULLIF(COUNT(*), 0), 2)                                         AS acceptance_percent
FROM {table}
WHERE LOWER(TRIM(term)) = 'fall 2026'
  AND llm_generated_university IS NOT NULL
  AND llm_generated_university <> 'Unknown'
GROUP BY llm_generated_university
ORDER BY entries DESC, university
LIMIT {limit}
""")

EXPLANATIONS = {
    "1": "Counts every row whose term, ignoring capitalization and surrounding spaces, is "
         "'Fall 2026'. The term column is the intended start term shown on each Grad Café entry.",
    "2": "The WHERE clause keeps only rows with a usable classification (International, American "
         "or Other), so missing and blank values are excluded from the denominator. FILTER counts "
         "the International rows among them, and the ratio is multiplied by 100 and rounded to "
         "two decimals. American and Other are part of the denominator but not the numerator. "
         "NULLIF avoids dividing by zero.",
    "3": "Each average has its own FILTER, so an applicant contributes to every metric they "
         "reported, independently of the other three. A value counts as reported only when it "
         "lies on the official scale: GPA above 0 and at most 4.0, GRE Verbal and Quantitative "
         "130 to 170, Analytical Writing 0 to 6. Many applicants type their 260-340 GRE total "
         "into the Quantitative field, or 99.99 into Analytical Writing; including those would "
         "produce averages that are not scores at all. AVG ignores NULLs, and "
         "ROUND(...::numeric, 2) rounds to two decimals.",
    "4": "Restricts the rows to Fall 2026 entries classified as American that report a GPA on the "
         "4.0 scale, then averages the GPA. COUNT(gpa) shows how many applicants the average is "
         "based on.",
    "5": "Takes all Fall 2025 entries as the denominator and counts, with FILTER, those whose "
         "cleaned status starts with 'accept' as the numerator. The percentage is rounded to two "
         "decimals.",
    "6": "Restricts the rows to Fall 2026 entries whose status indicates an acceptance and that "
         "report a GPA on the 4.0 scale, then averages the GPA.",
    "7": "Uses the original downloaded fields only. The program column holds the text "
         "'Program, University', so one case-insensitive regular expression recognizes Johns "
         "Hopkins (also 'John Hopkins' and the acronym JHU as a whole word) and another "
         "recognizes Computer Science (also the EECS acronym). The degree column must start "
         "with 'Master'.",
    "8": "Applies all five restrictions at once: term Fall 2026, an accepted status, a PhD degree "
         "(PhD, Ph.D.), a Computer Science program (including 'Electrical Engineering and "
         "Computer Science', which is how MIT lists its CS PhD, and the EECS acronym), and one of "
         "the four universities recognized in the original program text. MIT is matched both by "
         "its full name and by the case-sensitive acronym.",
    "9": "Keeps the term, status and degree restrictions of Question 8 in the WHERE clause and "
         "computes both counts in one pass with FILTER: the first uses the original program text "
         "exactly as in Question 8, the second uses the LLM-generated program and the "
         "LLM-generated university, recognized with the same school patterns as Question 8, so a "
         "standardized name written as an acronym or short form ('MIT', 'Stanford') counts as "
         "well as the full name. The difference is LLM count minus original count. Why the counts "
         "can agree: Grad Café's current submission form has applicants pick their university "
         "from a list, so these four schools already arrive with one spelling each (the LLM step "
         "only removes the '(MIT)' suffix), and their Computer Science program names are short "
         "and consistent, so both approaches select the same entries. The fields would diverge on "
         "free-text entries: a misspelled or unusually written school or program that the "
         "patterns above miss (for example 'Stanferd', 'Carnegie-Mellon', or 'CS' without the "
         "words 'Computer Science') is caught by the LLM field only when the standardizer maps it "
         "to one of these names, while a hallucinated or over-merged LLM name can add or drop an "
         "entry that the original text classifies correctly.",
    "10": "Groups Fall 2026 entries by degree type, keeps only groups with at least 100 entries "
          "(HAVING), and for each group computes the number of entries, the number and percentage "
          "of acceptances, and the average GPA of the accepted applicants who report a GPA on the "
          "4.0 scale.",
    "11": "Groups Fall 2026 entries by the LLM-standardized university name, which merges most "
          "spelling variants of the same school, ignores rows the standardizer could not "
          "attribute ('Unknown'), sorts by the number of entries and keeps the top ten, with the "
          "share of each school's entries that report an acceptance. A few small leftovers stay "
          "separate groups (for example 'Yale' or 'Stanford' written alone, each with fewer than "
          "ten rows); they are too small to change which schools make the top ten.",
}

# --------------------------------------------------------------------------- #
#                                  Answering                                  #
# --------------------------------------------------------------------------- #


def _one(cur: psycopg.Cursor, number: str, statement: sql.Composed) -> dict:
    cur.execute(statement, statement_params(number))
    return cur.fetchone() or {}


def _all(cur: psycopg.Cursor, number: str, statement: sql.Composed) -> list[dict]:
    cur.execute(statement, statement_params(number))
    return cur.fetchall()


def _answer(cur: psycopg.Cursor, number: str, statement: sql.Composed,
            lines: list[tuple[str, str]], **extra) -> Answer:
    text = statement.as_string(cur).strip()
    return Answer(number, QUESTIONS[number], text, EXPLANATIONS[number], lines, **extra)


def answer_q1(cur: psycopg.Cursor) -> Answer:
    """Question 1: run SQL_Q1 and format the result as an Answer."""
    row = _one(cur, "1", SQL_Q1)
    return _answer(cur, "1", SQL_Q1, [
        ("Fall 2026 applicant count", format_count(row["fall_2026_entries"])),
    ])


def answer_q2(cur: psycopg.Cursor) -> Answer:
    """Question 2: run SQL_Q2 and format the result as an Answer."""
    row = _one(cur, "2", SQL_Q2)
    return _answer(cur, "2", SQL_Q2, [
        ("Percent international", format_percent(row["percent_international"])),
        ("International entries", format_count(row["international_entries"])),
        ("Entries with a nationality classification", format_count(row["classified_entries"])),
    ])


def answer_q3(cur: psycopg.Cursor) -> Answer:
    """Question 3: run SQL_Q3 and format the result as an Answer."""
    row = _one(cur, "3", SQL_Q3)
    return _answer(cur, "3", SQL_Q3, [
        ("Average GPA", format_average_with_count(row["avg_gpa"], row["gpa_n"])),
        ("Average GRE Quantitative",
         format_average_with_count(row["avg_gre_quant"], row["gre_quant_n"])),
        ("Average GRE Verbal",
         format_average_with_count(row["avg_gre_verbal"], row["gre_verbal_n"])),
        ("Average GRE Analytical Writing",
         format_average_with_count(row["avg_gre_aw"], row["gre_aw_n"])),
    ])


def answer_q4(cur: psycopg.Cursor) -> Answer:
    """Question 4: run SQL_Q4 and format the result as an Answer."""
    row = _one(cur, "4", SQL_Q4)
    return _answer(cur, "4", SQL_Q4, [
        ("Average GPA of American Fall 2026 applicants", format_average(row["avg_gpa"])),
        ("Applicants with a GPA", format_count(row["applicants_with_gpa"])),
    ])


def answer_q5(cur: psycopg.Cursor) -> Answer:
    """Question 5: run SQL_Q5 and format the result as an Answer."""
    row = _one(cur, "5", SQL_Q5)
    return _answer(cur, "5", SQL_Q5, [
        ("Fall 2025 acceptance percentage", format_percent(row["acceptance_percent"])),
        ("Accepted Fall 2025 entries", format_count(row["accepted_entries"])),
        ("All Fall 2025 entries", format_count(row["fall_2025_entries"])),
    ])


def answer_q6(cur: psycopg.Cursor) -> Answer:
    """Question 6: run SQL_Q6 and format the result as an Answer."""
    row = _one(cur, "6", SQL_Q6)
    return _answer(cur, "6", SQL_Q6, [
        ("Average GPA of accepted Fall 2026 applicants", format_average(row["avg_gpa"])),
        ("Applicants with a GPA", format_count(row["applicants_with_gpa"])),
    ])


def answer_q7(cur: psycopg.Cursor) -> Answer:
    """Question 7: run SQL_Q7 and format the result as an Answer."""
    row = _one(cur, "7", SQL_Q7)
    return _answer(cur, "7", SQL_Q7, [
        ("JHU Computer Science master's entries", format_count(row["jhu_cs_masters_entries"])),
    ])


def answer_q8(cur: psycopg.Cursor) -> Answer:
    """Question 8: run SQL_Q8 and format the result as an Answer."""
    row = _one(cur, "8", SQL_Q8)
    return _answer(cur, "8", SQL_Q8, [
        ("Accepted Fall 2026 CS PhD entries (original fields)",
         format_count(row["accepted_cs_phd_entries"])),
    ])


def answer_q9(cur: psycopg.Cursor) -> Answer:
    """Question 9: run SQL_Q9 and format the result as an Answer."""
    row = _one(cur, "9", SQL_Q9)
    original, llm = row["original_field_count"], row["llm_field_count"]
    return _answer(cur, "9", SQL_Q9, [
        ("Original-field count", format_count(original)),
        ("LLM-field count", format_count(llm)),
        ("Difference", format_difference(llm - original)),
    ])


def answer_q10(cur: psycopg.Cursor) -> Answer:
    """Question 10: run SQL_Q10 and format the result as an Answer."""
    table = [
        [r["degree"], format_count(r["entries"]), format_count(r["acceptances"]),
         format_percent(r["acceptance_percent"]), format_average(r["avg_accepted_gpa"])]
        for r in _all(cur, "10", SQL_Q10)
    ]
    columns = ["Degree", "Entries", "Acceptances", "Acceptance %", "Avg GPA (accepted)"]
    return _answer(cur, "10", SQL_Q10, [], columns=columns, table=table)


def answer_q11(cur: psycopg.Cursor) -> Answer:
    """Question 11: run SQL_Q11 and format the result as an Answer."""
    table = [
        [r["university"], format_count(r["entries"]), format_count(r["acceptances"]),
         format_percent(r["acceptance_percent"])]
        for r in _all(cur, "11", SQL_Q11)
    ]
    columns = ["University", "Entries", "Acceptances", "Acceptance %"]
    return _answer(cur, "11", SQL_Q11, [], columns=columns, table=table)


ANSWER_FUNCTIONS = (answer_q1, answer_q2, answer_q3, answer_q4, answer_q5, answer_q6,
                    answer_q7, answer_q8, answer_q9, answer_q10, answer_q11)


def run_all(conn: psycopg.Connection) -> list[Answer]:
    """Run every question inside one read-only transaction (a consistent snapshot)."""
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql.SQL("SET TRANSACTION READ ONLY"))
            return [function(cur) for function in ANSWER_FUNCTIONS]


# --------------------------------------------------------------------------- #
#                                   Console                                   #
# --------------------------------------------------------------------------- #


def main() -> int:
    """Console entry point: print all eleven answers.

    Returns the exit code: 0, 2 (cannot connect) or 3 (query failed); the
    connection and its error messages are handled by db_config.run_with_connection.
    """
    status, answers = run_with_connection(run_all)
    if status:
        return status
    print_answers(answers, "Grad Café analysis (raw SQL via psycopg)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
