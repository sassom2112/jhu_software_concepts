"""
db_config.py - One place that knows how to reach PostgreSQL.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

No credentials live in this repository.  The connection is described by
environment variables, in this order of preference:

  1. DATABASE_URL: a URL such as  postgresql://gradcafe@localhost:5432/gradcafe
     (a libpq "key=value" string such as  host=localhost dbname=gradcafe  and the
     SQLAlchemy spelling postgresql+psycopg://... also work).  When it is set it
     is used as it is, and nothing below is consulted.
  2. The project's own DB_HOST, DB_PORT, DB_NAME, DB_USER and DB_PASSWORD: how
     a deployment names its connection, e.g. the web or worker service logging
     in as the least-privilege role that db_roles.py creates for it (see
     .env.example).
  3. The standard libpq variables PGHOST, PGPORT, PGUSER, PGDATABASE.
  4. The defaults: localhost, 5432, user gradcafe, database gradcafe.

Host, port, user and database name are looked up one at a time, so DB_HOST can
be combined with PGUSER, and whatever is still missing falls back to its
default.  A host may also be a Unix-socket directory such as /var/run/postgresql.

DB_PASSWORD, when it is set, is handed to libpq inside the connection string;
it is never printed, and describe_target() leaves it out.  Without it, libpq
finds the password on its own, in ~/.pgpass (chmod 600) or PGPASSWORD, so it
never has to appear in a URL, a file in the repository, or a shell history.
Both psycopg (load_data.py, query_data.py) and SQLAlchemy's psycopg driver
(worker/etl/models.py, which builds its URL from get_database_url()) go through
libpq, so the same settings serve every part of the project.  This module does
not import SQLAlchemy: the web image installs only Flask, psycopg and pika.

The command-line tools that talk to PostgreSQL through psycopg
(query_data.py, build_query_results.py, db_roles.py) open their connection with
run_with_connection(), so they report an unusable setting, an unreachable
server and a failed query with the same messages and exit codes.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from typing import TypeVar

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DEFAULT_HOST = "localhost"
DEFAULT_PORT = "5432"
DEFAULT_USER = "gradcafe"
DEFAULT_DATABASE = "gradcafe"
CONNECT_TIMEOUT_SECONDS = 10

# The three tables of the schema (load_data.create_schema creates them):
TABLE_NAME = "applicants"                  # one row per Grad Cafe entry
WATERMARK_TABLE = "ingestion_watermarks"   # how far each source has been read
SNAPSHOT_TABLE = "analysis_snapshot"       # the analysis the web page shows

INVALID_SETTINGS_MESSAGE = (
    "error: the database connection settings are not valid; check DATABASE_URL, "
    "DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD or PGHOST/PGPORT/PGUSER/PGDATABASE "
    "(see README)"
)

# "postgresql+psycopg://" (SQLAlchemy style) is reduced to the scheme libpq understands.
_DRIVER_SUFFIX = re.compile(r"^(postgres(?:ql)?)\+\w+://", re.IGNORECASE)

T = TypeVar("T")


# Each connection field: (libpq keyword, the project's variable, libpq's own variable, default).
_FIELDS = (
    ("host", "DB_HOST", "PGHOST", DEFAULT_HOST),
    ("port", "DB_PORT", "PGPORT", DEFAULT_PORT),
    ("user", "DB_USER", "PGUSER", DEFAULT_USER),
    ("dbname", "DB_NAME", "PGDATABASE", DEFAULT_DATABASE),
)


def _settings() -> dict[str, str]:
    """Host, port, user and dbname (DB_* first, then PG*, then the default), plus
    DB_PASSWORD when it is set.  A blank DB_* value counts as not set."""
    settings = {
        keyword: os.environ.get(project_name, "").strip() or os.environ.get(libpq_name, default)
        for keyword, project_name, libpq_name, default in _FIELDS
    }
    password = os.environ.get("DB_PASSWORD", "")
    if password:
        settings["password"] = password
    return settings


def get_database_url() -> str:
    """Connection string for psycopg.

    DATABASE_URL (a URL or a key=value string) when it is set, else a key=value
    DSN built from the DB_* and PG* variables.  The result can hold DB_PASSWORD,
    so it is handed to libpq and never printed.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return _DRIVER_SUFFIX.sub(r"\1://", url, count=1)
    return make_conninfo(**_settings())


def describe_target() -> str:
    """``user@host:port/dbname`` for messages.

    Never raises, and never shows a password or the raw setting.
    """
    try:
        params = conninfo_to_dict(get_database_url())
    except (psycopg.ProgrammingError, ValueError):
        return "<unparseable DATABASE_URL>"
    user = params.get("user") or DEFAULT_USER
    host = params.get("host") or DEFAULT_HOST
    port = params.get("port") or DEFAULT_PORT
    dbname = params.get("dbname") or DEFAULT_DATABASE
    return f"{user}@{host}:{port}/{dbname}"


def run_with_connection(work: Callable[[psycopg.Connection], T]) -> tuple[int, T | None]:
    """Run ``work(conn)`` on a new psycopg connection, for a command-line tool.

    Returns ``(0, result)`` when everything worked.  Otherwise the reason is
    printed to stderr and the pair is ``(exit code, None)``: 2 when the
    settings cannot be parsed (they are never echoed) or the server cannot be
    reached, 3 when a query fails.  The connection commits if *work* returns,
    rolls back if it raises, and is closed either way.
    """
    try:
        conn = psycopg.connect(get_database_url(), connect_timeout=CONNECT_TIMEOUT_SECONDS)
    except psycopg.ProgrammingError:  # unparseable settings; do not echo them
        print(INVALID_SETTINGS_MESSAGE, file=sys.stderr)
        return 2, None
    except psycopg.OperationalError as err:
        print(f"error: cannot connect to PostgreSQL at {describe_target()}: {err}", file=sys.stderr)
        return 2, None
    try:
        with conn:
            return 0, work(conn)
    except psycopg.Error as err:
        print(f"error: query failed: {err}", file=sys.stderr)
        return 3, None
