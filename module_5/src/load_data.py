"""
load_data.py - Load the cleaned Grad Cafe data into PostgreSQL.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

Reads the Module 2 output (llm_extend_applicant_data.json by default: the
cleaned records plus the LLM-standardized program and university) and loads it
into a single table, `applicants`, using psycopg 3.

Design:
  * The table is created if it does not exist (see CREATE_TABLE_SQL).
  * p_id is Grad Cafe's own result id (the number in the entry's URL), so it is
    a natural, stable primary key: the same entry always gets the same p_id.
  * Rows are bulk-copied into a temporary staging table and then inserted with
    INSERT ... ON CONFLICT (p_id) DO NOTHING.  Running the loader again, or
    loading a file that overlaps the database, never creates duplicates and
    never overwrites rows that are already stored.
  * Missing values become SQL NULL; numbers that cannot be parsed become NULL
    instead of aborting the load; NUL characters (which PostgreSQL text cannot
    store) are removed from strings.
  * The whole load is one transaction: it either completes or changes nothing.

Usage::

    python load_data.py                                   # llm_extend_applicant_data.json
    python load_data.py --file applicant_data.json        # works without LLM columns too
    python load_data.py --reset                           # drop and recreate the table first

Connection settings come from the environment (see db_config.py); no password
is stored in this repository.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import date
from pathlib import Path
from typing import Iterable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from db_config import CONNECT_TIMEOUT_SECONDS, INVALID_SETTINGS_MESSAGE, TABLE_NAME, describe_target, get_database_url
from scrape import load_data as load_json  # Module 2 JSON reader (plain or .gz)

# The module folder (module_4/, the parent of src/): the command line reads and writes its
# data files there, next to src/ rather than inside it, as in Modules 2 and 3.
HERE = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = "llm_extend_applicant_data.json"

# Column order used everywhere in this file.
COLUMNS = (
    "p_id",
    "program",
    "comments",
    "date_added",
    "url",
    "status",
    "term",
    "us_or_international",
    "gpa",
    "gre",
    "gre_v",
    "gre_aw",
    "degree",
    "llm_generated_program",
    "llm_generated_university",
)

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    p_id                     INTEGER PRIMARY KEY,
    program                  TEXT,
    comments                 TEXT,
    date_added               DATE,
    url                      TEXT,
    status                   TEXT,
    term                     TEXT,
    us_or_international      TEXT,
    gpa                      FLOAT,
    gre                      FLOAT,
    gre_v                    FLOAT,
    gre_aw                   FLOAT,
    degree                   TEXT,
    llm_generated_program    TEXT,
    llm_generated_university TEXT
)
"""


# --------------------------------------------------------------------------- #
#                         Record -> row conversion                            #
# --------------------------------------------------------------------------- #


def _text(value: object) -> str | None:
    """Trimmed text without NUL characters; empty or missing -> None."""
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    return text or None


def _float(value: object) -> float | None:
    """A finite float, or None for missing / unparseable / NaN values."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _date(value: object) -> date | None:
    """ISO 'YYYY-MM-DD' (optionally followed by a time) -> date; otherwise None."""
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _p_id(record: dict) -> int | None:
    """Grad Cafe's result id: the `result_id` key, or the number at the end of the URL."""
    for candidate in (record.get("p_id"), record.get("result_id")):
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            pass
    url = _text(record.get("url")) or ""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def record_to_row(record: dict) -> tuple | None:
    """Map one cleaned Module 2 record onto the applicants columns (None if unusable).

    `program` keeps the combined "Program, University" text from Module 2; for
    the few entries whose program name is blank on Grad Cafe it falls back to
    the university so the column still identifies the school.  The LLM columns
    accept both the Module 2 spelling (llm-generated-program) and the column
    spelling (llm_generated_program).
    """
    p_id = _p_id(record)
    if p_id is None:
        return None
    program = _text(record.get("program")) or _text(record.get("university"))
    return (
        p_id,
        program,
        _text(record.get("comments")),
        _date(record.get("date_added")),
        _text(record.get("url")),
        _text(record.get("status")),
        _text(record.get("term")),
        _text(record.get("us_or_international")),
        _float(record.get("gpa")),
        _float(record.get("gre")),
        _float(record.get("gre_v")),
        _float(record.get("gre_aw")),
        _text(record.get("degree")),
        _text(record.get("llm-generated-program", record.get("llm_generated_program"))),
        _text(record.get("llm-generated-university", record.get("llm_generated_university"))),
    )


# --------------------------------------------------------------------------- #
#                               Database work                                 #
# --------------------------------------------------------------------------- #


def connect() -> psycopg.Connection:
    """Open a psycopg connection using the environment (see db_config.py)."""
    return psycopg.connect(get_database_url(), connect_timeout=CONNECT_TIMEOUT_SECONDS)


def create_table(conn: psycopg.Connection, reset: bool = False) -> None:
    """Create the applicants table (optionally dropping it first)."""
    with conn.cursor() as cur:
        if reset:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(TABLE_NAME)))
        cur.execute(CREATE_TABLE_SQL)


def load_records(conn: psycopg.Connection, records: Iterable[dict]) -> tuple[int, int, int]:
    """Insert records that are not in the table yet.

    Returns (inserted, already_present, unusable).  Existing rows are never
    modified.  The caller controls the transaction.
    """
    rows: dict[int, tuple] = {}
    unusable = 0
    for record in records:
        row = record_to_row(record) if isinstance(record, dict) else None
        if row is None:
            unusable += 1
            continue
        rows.setdefault(row[0], row)  # first occurrence wins inside one file

    column_list = sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TEMP TABLE applicants_staging (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP")
            .format(sql.Identifier(TABLE_NAME))
        )
        with cur.copy(sql.SQL("COPY applicants_staging ({}) FROM STDIN").format(column_list)) as copy:
            for row in rows.values():
                copy.write_row(row)
        cur.execute(
            sql.SQL("INSERT INTO {} ({cols}) SELECT {cols} FROM applicants_staging ON CONFLICT (p_id) DO NOTHING")
            .format(sql.Identifier(TABLE_NAME), cols=column_list)
        )
        inserted = cur.rowcount
        cur.execute("DROP TABLE applicants_staging")
    return inserted, len(rows) - inserted, unusable


def count_rows(conn: psycopg.Connection) -> int:
    """Number of rows currently stored in the applicants table."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(TABLE_NAME)))
        return cur.fetchone()[0]


def fetch_applicants(conn: psycopg.Connection) -> list[dict]:
    """Every stored row as a dict keyed by the Module 3 column names, newest p_id first."""
    column_list = sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS)
    query = sql.SQL("SELECT {} FROM {} ORDER BY p_id DESC").format(column_list, sql.Identifier(TABLE_NAME))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query)
        return cur.fetchall()


# --------------------------------------------------------------------------- #
#                                Command line                                 #
# --------------------------------------------------------------------------- #


def _local_file(name: str) -> Path:
    """Input files are looked up in the module folder (HERE) by bare name (no path traversal)."""
    base = os.path.basename(os.path.normpath(name))
    if not base or base in (".", ".."):
        raise ValueError(f"not a usable file name: {name!r}")
    return HERE / base


def main(argv: list[str] | None = None) -> int:
    """Command line: load a JSON file; returns 0, 1 (unreadable input), 2 (cannot connect) or 3 (rolled back)."""
    parser = argparse.ArgumentParser(description="Load cleaned Grad Cafe data into PostgreSQL")
    parser.add_argument("--file", default=DEFAULT_INPUT,
                        help=f"JSON (or .json.gz) file name in the module folder (default {DEFAULT_INPUT})")
    parser.add_argument("--reset", action="store_true",
                        help="drop and recreate the applicants table before loading")
    args = parser.parse_args(argv)

    try:
        path = _local_file(args.file)
        records = load_json(path)
    except (OSError, ValueError) as err:
        print(f"error: cannot read input: {err}", file=sys.stderr)
        return 1

    try:
        conn = connect()
    except psycopg.ProgrammingError:  # the settings could not even be parsed; do not echo them
        print(INVALID_SETTINGS_MESSAGE, file=sys.stderr)
        return 2
    except psycopg.OperationalError as err:
        print(f"error: cannot connect to PostgreSQL at {describe_target()}: {err}".strip(), file=sys.stderr)
        print("hint: start the database and set DATABASE_URL or PGHOST/PGUSER/PGDATABASE (see README)", file=sys.stderr)
        return 2

    try:
        with conn:  # commits on success, rolls back on any exception, then closes
            create_table(conn, reset=args.reset)
            inserted, present, unusable = load_records(conn, records)
            total = count_rows(conn)
    except psycopg.Error as err:
        print(f"error: the load was rolled back and nothing was changed: {err}", file=sys.stderr)
        return 3

    print(f"Read {len(records):,} records from {path.name}")
    print(f"Inserted {inserted:,} new rows; {present:,} were already present; {unusable:,} unusable records skipped")
    print(f"applicants table now holds {total:,} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
