"""
build_query_results.py - Produce query_results.html (and from it, query_results.pdf).

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

Runs every question in query_data.py against the live database and writes an
HTML report with, for each question: the question in words, the result, the
exact SQL that query_data.py executes, and an explanation.  Print the HTML to
PDF with any browser (File > Print > Save as PDF), or headless Chrome:

    python build_query_results.py
    google-chrome --headless --no-pdf-header-footer --print-to-pdf=query_results.pdf query_results.html
"""

from __future__ import annotations

import html
import sys
from datetime import datetime
from pathlib import Path

import psycopg

import analysis_common as rules
import query_data
from db_config import CONNECT_TIMEOUT_SECONDS, INVALID_SETTINGS_MESSAGE, describe_target, get_database_url

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "query_results.html"

CSS = """
@page { size: Letter; margin: 0.6in 0.65in; }
body { font: 10.5pt/1.45 "Segoe UI", Roboto, Arial, sans-serif; color: #1d2433; }
h1 { font-size: 19pt; margin: 0 0 2pt; color: #002d72; }
.sub { color: #5b6475; margin: 0 0 14pt; }
.meta { border: 1px solid #dfe3ea; border-radius: 6pt; padding: 8pt 10pt; margin-bottom: 16pt; background: #f7f9fc; }
.meta p { margin: 2pt 0; }
section { break-inside: avoid-page; border-top: 2px solid #002d72; padding-top: 8pt; margin-bottom: 16pt; }
h2 { font-size: 12.5pt; margin: 0 0 4pt; color: #002d72; }
.question { font-weight: 600; margin: 0 0 6pt; }
.label { font-size: 8pt; text-transform: uppercase; letter-spacing: .06em; color: #5b6475; margin: 8pt 0 2pt; }
.result { background: #e7eef9; border-radius: 5pt; padding: 6pt 9pt; }
.result p { margin: 1pt 0; }
.result strong { color: #002d72; }
pre { background: #f5f6f8; border: 1px solid #dfe3ea; border-radius: 5pt; padding: 7pt 9pt; font: 8.6pt/1.35 Consolas, "DejaVu Sans Mono", monospace; white-space: pre-wrap; margin: 0; }
table { border-collapse: collapse; width: 100%; font-size: 9.5pt; }
th, td { border-bottom: 1px solid #c9d3e3; padding: 3pt 6pt; text-align: right; }
th:first-child, td:first-child { text-align: left; }
th { color: #5b6475; font-weight: 600; }
.explain { margin: 0; }
"""


def _answer_html(answer: query_data.Answer) -> str:
    parts = [f"<section><h2>Question {answer.number}</h2>",
             f"<p class='question'>{html.escape(answer.question)}</p>",
             "<p class='label'>Result</p><div class='result'>"]
    for index, (label, value) in enumerate(answer.lines):
        number, separator, rest = value.partition(" (")  # bold the number, not "(n = ...)"
        value_html = html.escape(value)
        if index == 0:
            value_html = f"<strong>{html.escape(number)}</strong>" + (f" ({html.escape(rest)}" if separator else "")
        parts.append(f"<p>{html.escape(label)}: {value_html}</p>")
    if answer.table:
        parts.append("<table><thead><tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in answer.columns) + "</tr></thead><tbody>")
        for row in answer.table:
            parts.append("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>")
        parts.append("</tbody></table>")
    parts.append("</div>")
    parts.append(f"<p class='label'>SQL query (as executed by query_data.py)</p><pre>{html.escape(answer.sql)}</pre>")
    parts.append(f"<p class='label'>What the query does</p><p class='explain'>{html.escape(answer.explanation)}</p></section>")
    return "\n".join(parts)


def _data_notes(conn: psycopg.Connection) -> str:
    """Live counts that justify the valid-range rule used for the averages."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*),
                   COUNT(gre) FILTER (WHERE gre NOT BETWEEN 130 AND 170),
                   COUNT(gre) FILTER (WHERE gre BETWEEN 260 AND 340),
                   COUNT(gre_aw) FILTER (WHERE gre_aw NOT BETWEEN 0 AND 6),
                   COUNT(gpa) FILTER (WHERE NOT (gpa > 0 AND gpa <= 4.0)),
                   ROUND(AVG(gre)::numeric, 2), MAX(date_added), MIN(date_added)
            FROM applicants
        """)
        total, bad_q, totals_q, bad_aw, bad_gpa, naive_q, newest, oldest = cur.fetchone()
    return (f"<p><b>Database:</b> {total:,} entries added between {oldest:%B %d, %Y} and {newest:%B %d, %Y}.</p>"
            f"<p><b>Valid-range rule for averages:</b> a score counts as provided only on its official scale "
            f"(GPA above {rules.GPA_MIN_EXCLUSIVE:g} and at most {rules.GPA_MAX:g}; GRE Verbal and Quantitative "
            f"{rules.GRE_SECTION_MIN:g}–{rules.GRE_SECTION_MAX:g}; Analytical Writing {rules.GRE_AW_MIN:g}–{rules.GRE_AW_MAX:g}). "
            f"In this database {bad_q:,} values in the GRE Quantitative column are off that scale, {totals_q:,} of them "
            f"260–340 composite totals; averaging the column as stored would give {naive_q}. Also excluded: {bad_aw:,} "
            f"Analytical Writing values and {bad_gpa:,} GPAs off their scales. Stored values are never modified.</p>")


def main() -> int:
    try:
        conn = psycopg.connect(get_database_url(), connect_timeout=CONNECT_TIMEOUT_SECONDS)
    except psycopg.ProgrammingError:
        print(INVALID_SETTINGS_MESSAGE, file=sys.stderr)
        return 2
    except psycopg.OperationalError as err:
        print(f"error: cannot connect to PostgreSQL at {describe_target()}: {err}", file=sys.stderr)
        return 2
    try:
        with conn:
            answers = query_data.run_all(conn)
            notes = _data_notes(conn)
    except psycopg.Error as err:
        print(f"error: query failed: {err}", file=sys.stderr)
        return 3
    body = "\n".join(_answer_html(a) for a in answers)
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Module 3 SQL Query Results</title>
<style>{CSS}</style></head><body>
<h1>Grad Café SQL Analysis: Query Results</h1>
<p class="sub">JHU EN.605.256 Modern Software Concepts in Python · Module 3 · generated {datetime.now():%B %d, %Y %H:%M}</p>
<div class="meta">{notes}
<p><b>Matching conventions:</b> text comparisons ignore capitalization and surrounding spaces; an acceptance is a status
starting with "accept"; regular expressions use PostgreSQL's ~* (case-insensitive) operator, where \\m and \\M mark word boundaries.</p></div>
{body}
</body></html>"""
    OUTPUT.write_text(document, encoding="utf-8")
    print(f"Wrote {OUTPUT.name} ({len(answers)} questions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
