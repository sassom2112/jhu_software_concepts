"""
load_data.py - Load the cleaned Grad Cafe data into PostgreSQL.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

Reads the Module 2 output (src/data/applicant_data.json by default: the
cleaned records plus the LLM-standardized program and university) and loads it
into the `applicants` table, using psycopg 3.  It also owns the rest of the
schema: create_schema() creates

  * applicants: one row per Grad Cafe entry (CREATE_TABLE_STATEMENT);
  * ingestion_watermarks: for each source, the newest result id the worker has
    already read (last_seen, stored as TEXT), so a pull fetches only newer
    entries; read with get_watermark(), moved with set_watermark() (until the
    first pull records one, newest_p_id() says where the stored data ends);
  * analysis_snapshot: the one row of analysis results the web page shows
    (db/snapshot.py reads and writes it).

Design:
  * Tables are created if they do not exist.  A table that already exists is
    left alone without issuing CREATE at all, so a role without the CREATE
    privilege on the schema (the web and worker roles) can call these
    functions safely.
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

Usage (``gradcafe-load`` is the same command once ``pip install -e .`` has run)::

    python -m db.load_data                                # src/data/applicant_data.json
    python -m db.load_data --file other.json              # works without LLM columns too
    python -m db.load_data --reset                        # drop and recreate the table first

--file takes a bare file name inside the data folder: src/data/, or the folder
named by the DATA_DIR environment variable.  Connection settings come from the
environment (see db_config.py); no password is stored in this repository.
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

from db.db_config import (CONNECT_TIMEOUT_SECONDS, INVALID_SETTINGS_MESSAGE, SNAPSHOT_TABLE,
                          TABLE_NAME, WATERMARK_TABLE, describe_target, get_database_url)
from db.jsonio import load_data as load_json  # Module 2 JSON reader (plain or .gz)
from db.query_limits import MAX_LIMIT, clamp_limit

# The data folder the command line reads its input from: src/data/ (src/ is the parent of
# this db/ package), unless the DATA_DIR environment variable names another folder.
DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parents[1] / "data")
DEFAULT_INPUT = "applicant_data.json"

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

# Column definitions of the Module 3 schema, in COLUMNS order.  The types are
# fixed SQL keywords written here in the code, never taken from any input.
COLUMN_TYPES = (
    ("p_id", "INTEGER PRIMARY KEY"),
    ("program", "TEXT"),
    ("comments", "TEXT"),
    ("date_added", "DATE"),
    ("url", "TEXT"),
    ("status", "TEXT"),
    ("term", "TEXT"),
    ("us_or_international", "TEXT"),
    ("gpa", "FLOAT"),
    ("gre", "FLOAT"),
    ("gre_v", "FLOAT"),
    ("gre_aw", "FLOAT"),
    ("degree", "TEXT"),
    ("llm_generated_program", "TEXT"),
    ("llm_generated_university", "TEXT"),
)
STAGING_TABLE = "applicants_staging"
WATERMARK_SOURCE = "gradcafe"   # the ingestion_watermarks row of the Grad Cafe scraper

# Built with psycopg's sql module: names are quoted with sql.Identifier, and no
# f-string, + or .format() ever puts text into a statement.
CREATE_TABLE_STATEMENT = sql.SQL("CREATE TABLE IF NOT EXISTS {table} ({columns})").format(
    table=sql.Identifier(TABLE_NAME),
    columns=sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(sql_type))
        for name, sql_type in COLUMN_TYPES
    ),
)
DROP_TABLE_STATEMENT = sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(TABLE_NAME))
# The watermark table exactly as the assignment specifies it.  last_seen is TEXT so any
# sort key fits; this project stores the newest Grad Cafe result id, e.g. '1020481'.
CREATE_WATERMARK_TABLE_STATEMENT = sql.SQL(
    "CREATE TABLE IF NOT EXISTS {table} ("
    "source TEXT PRIMARY KEY, "
    "last_seen TEXT, "
    "updated_at TIMESTAMPTZ DEFAULT now())"
).format(table=sql.Identifier(WATERMARK_TABLE))
# One row (id = 1) holding every number the analysis page shows, as JSON.  The worker
# replaces it after each change; the web app only reads it (see db/snapshot.py).
CREATE_SNAPSHOT_TABLE_STATEMENT = sql.SQL(
    "CREATE TABLE IF NOT EXISTS {table} ("
    "id SMALLINT PRIMARY KEY CHECK (id = 1), "
    "computed_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
    "payload JSONB NOT NULL)"
).format(table=sql.Identifier(SNAPSHOT_TABLE))
# Each table and the statement that creates it, in creation order.
SCHEMA_STATEMENTS = (
    (TABLE_NAME, CREATE_TABLE_STATEMENT),
    (WATERMARK_TABLE, CREATE_WATERMARK_TABLE_STATEMENT),
    (SNAPSHOT_TABLE, CREATE_SNAPSHOT_TABLE_STATEMENT),
)
# to_regclass() returns NULL for a table that does not exist, instead of failing.
TABLE_EXISTS_STATEMENT = sql.SQL("SELECT to_regclass({name}) IS NOT NULL LIMIT 1").format(
    name=sql.Placeholder("name")
)
GET_WATERMARK_STATEMENT = sql.SQL(
    "SELECT last_seen FROM {table} WHERE source = {source} LIMIT 1"
).format(table=sql.Identifier(WATERMARK_TABLE), source=sql.Placeholder("source"))
SET_WATERMARK_STATEMENT = sql.SQL(
    "INSERT INTO {table} (source, last_seen, updated_at) VALUES ({source}, {last_seen}, now()) "
    "ON CONFLICT (source) DO UPDATE SET last_seen = EXCLUDED.last_seen, "
    "updated_at = EXCLUDED.updated_at"
).format(table=sql.Identifier(WATERMARK_TABLE), source=sql.Placeholder("source"),
         last_seen=sql.Placeholder("last_seen"))
_COLUMN_LIST = sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS)
_NAMES = {
    "table": sql.Identifier(TABLE_NAME),
    "staging": sql.Identifier(STAGING_TABLE),
    "cols": _COLUMN_LIST,
    "key": sql.Identifier("p_id"),
}
STAGING_CREATE_STATEMENT = sql.SQL(
    "CREATE TEMP TABLE {staging} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP"
).format(**_NAMES)
STAGING_COPY_STATEMENT = sql.SQL("COPY {staging} ({cols}) FROM STDIN").format(**_NAMES)
# LIMIT is the exact size of this batch: it bounds the statement without ever dropping a row.
INSERT_NEW_ROWS_STATEMENT = sql.SQL(
    "INSERT INTO {table} ({cols}) SELECT {cols} FROM {staging} LIMIT {batch} "
    "ON CONFLICT ({key}) DO NOTHING"
).format(batch=sql.Placeholder("batch"), **_NAMES)
STAGING_DROP_STATEMENT = sql.SQL("DROP TABLE {staging}").format(**_NAMES)
COUNT_ROWS_STATEMENT = sql.SQL("SELECT COUNT(*) FROM {table} LIMIT {limit}").format(
    limit=sql.Placeholder("limit"), **_NAMES
)
NEWEST_ID_STATEMENT = sql.SQL("SELECT MAX({key}) FROM {table} LIMIT {limit}").format(
    limit=sql.Placeholder("limit"), **_NAMES
)
FETCH_APPLICANTS_STATEMENT = sql.SQL(
    "SELECT {cols} FROM {table} ORDER BY {key} DESC LIMIT {limit}"
).format(limit=sql.Placeholder("limit"), **_NAMES)


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
    text = _text(value) or ""  # missing -> "", which fromisoformat rejects like any bad date
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


def table_exists(conn: psycopg.Connection, name: str) -> bool:
    """True if a table called *name* is visible on the search path (to_regclass)."""
    with conn.cursor() as cur:
        cur.execute(TABLE_EXISTS_STATEMENT, {"name": name})
        return bool(cur.fetchone()[0])


def _create_if_missing(conn: psycopg.Connection, name: str, statement: sql.Composed) -> None:
    """Run the CREATE TABLE *statement* only when table *name* does not exist yet.

    Checking first matters: PostgreSQL checks the CREATE privilege on the schema
    before it looks at IF NOT EXISTS, so even CREATE TABLE IF NOT EXISTS fails for
    a role without that privilege, although the table is already there.
    """
    if not table_exists(conn, name):
        conn.execute(statement)


def create_table(conn: psycopg.Connection, reset: bool = False) -> None:
    """Create the applicants table if it is missing (with *reset*, drop it first)."""
    if reset:
        conn.execute(DROP_TABLE_STATEMENT)
    _create_if_missing(conn, TABLE_NAME, CREATE_TABLE_STATEMENT)


def create_schema(conn: psycopg.Connection, reset: bool = False) -> None:
    """Create every missing table: applicants, ingestion_watermarks and analysis_snapshot.

    *reset* drops and recreates applicants only (see create_table); the other
    two tables are never dropped here.
    """
    create_table(conn, reset=reset)
    for name, statement in SCHEMA_STATEMENTS[1:]:
        _create_if_missing(conn, name, statement)


def get_watermark(conn: psycopg.Connection, source: str = WATERMARK_SOURCE) -> str | None:
    """The last_seen value stored for *source*, or None if it has never been set."""
    with conn.cursor() as cur:
        cur.execute(GET_WATERMARK_STATEMENT, {"source": source})
        row = cur.fetchone()
    return row[0] if row else None


def set_watermark(conn: psycopg.Connection, last_seen: str,
                  source: str = WATERMARK_SOURCE) -> None:
    """Store *last_seen* for *source* (insert or update, updated_at = now()).

    The value is stored as given: the caller decides it may only move forward.
    The caller controls the transaction, so the watermark commits together
    with the rows it describes, or not at all.
    """
    with conn.cursor() as cur:
        cur.execute(SET_WATERMARK_STATEMENT, {"source": source, "last_seen": str(last_seen)})


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

    with conn.cursor() as cur:
        cur.execute(STAGING_CREATE_STATEMENT)
        with cur.copy(STAGING_COPY_STATEMENT) as copy:
            for row in rows.values():
                copy.write_row(row)
        cur.execute(INSERT_NEW_ROWS_STATEMENT, {"batch": len(rows)})
        inserted = cur.rowcount
        cur.execute(STAGING_DROP_STATEMENT)
    return inserted, len(rows) - inserted, unusable


def count_rows(conn: psycopg.Connection) -> int:
    """Number of rows currently stored in the applicants table."""
    with conn.cursor() as cur:
        cur.execute(COUNT_ROWS_STATEMENT, {"limit": 1})
        return cur.fetchone()[0]


def newest_p_id(conn: psycopg.Connection) -> int | None:
    """The highest stored p_id (Grad Cafe's newest result id), or None for an empty table.

    Grad Cafe numbers its results in the order they are posted, so this is
    where the stored data ends; the worker starts from it while no watermark
    has been recorded yet."""
    with conn.cursor() as cur:
        cur.execute(NEWEST_ID_STATEMENT, {"limit": 1})
        return cur.fetchone()[0]


def fetch_applicants(conn: psycopg.Connection, limit: object = MAX_LIMIT) -> list[dict]:
    """The newest stored rows (at most *limit*, clamped to 1..MAX_LIMIT) as dicts keyed by
    the Module 3 column names, newest p_id first."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(FETCH_APPLICANTS_STATEMENT, {"limit": clamp_limit(limit, default=MAX_LIMIT)})
        return cur.fetchall()


# --------------------------------------------------------------------------- #
#                                Command line                                 #
# --------------------------------------------------------------------------- #


def input_path(name: str) -> Path:
    """Input files are looked up in the data folder (DATA_DIR) by bare name (no path traversal)."""
    base = os.path.basename(os.path.normpath(name))
    if not base or base in (".", ".."):
        raise ValueError(f"not a usable file name: {name!r}")
    return DATA_DIR / base


def read_input(name: str) -> tuple[Path, list[dict]]:
    """(path, records) of the JSON input file *name* in the data folder.

    Raises OSError (missing or unreadable) or ValueError (an unusable name, or
    not a JSON list)."""
    path = input_path(name)
    return path, load_json(path)


def main(argv: list[str] | None = None) -> int:
    """Command line: load a JSON file.

    Returns 0, 1 (unreadable input), 2 (cannot connect) or 3 (rolled back).
    """
    parser = argparse.ArgumentParser(description="Load cleaned Grad Cafe data into PostgreSQL")
    parser.add_argument("--file", default=DEFAULT_INPUT,
                        help="JSON (or .json.gz) file name in the data folder "
                             f"(default {DEFAULT_INPUT})")
    parser.add_argument("--reset", action="store_true",
                        help="drop and recreate the applicants table before loading "
                             "(the watermark and snapshot tables are kept)")
    args = parser.parse_args(argv)

    try:
        path, records = read_input(args.file)
    except (OSError, ValueError) as err:
        print(f"error: cannot read input: {err}", file=sys.stderr)
        return 1

    try:
        conn = connect()
    except psycopg.ProgrammingError:  # the settings could not even be parsed; do not echo them
        print(INVALID_SETTINGS_MESSAGE, file=sys.stderr)
        return 2
    except psycopg.OperationalError as err:
        print(f"error: cannot connect to PostgreSQL at {describe_target()}: {err}".strip(),
              file=sys.stderr)
        print("hint: start the database and set DATABASE_URL, DB_HOST/DB_USER/DB_NAME or "
              "PGHOST/PGUSER/PGDATABASE (see README)", file=sys.stderr)
        return 2

    try:
        with conn:  # commits on success, rolls back on any exception, then closes
            create_schema(conn, reset=args.reset)
            inserted, present, unusable = load_records(conn, records)
            total = count_rows(conn)
    except psycopg.Error as err:
        print(f"error: the load was rolled back and nothing was changed: {err}", file=sys.stderr)
        return 3

    print(f"Read {len(records):,} records from {path.name}")
    print(f"Inserted {inserted:,} new rows; {present:,} were already present; "
          f"{unusable:,} unusable records skipped")
    print(f"applicants table now holds {total:,} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
