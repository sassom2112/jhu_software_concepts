"""
db_config.py - One place that knows how to reach PostgreSQL.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

No credentials live in this repository.  The connection is described by
environment variables, in this order of preference:

  1. DATABASE_URL: a URL such as  postgresql://gradcafe@localhost:5432/gradcafe
     (a libpq "key=value" string such as  host=localhost dbname=gradcafe  and the
     SQLAlchemy spelling postgresql+psycopg://... also work).
  2. The standard libpq variables PGHOST, PGPORT, PGUSER, PGDATABASE
     (defaults: localhost, 5432, gradcafe, gradcafe).  PGHOST may also be a
     Unix-socket directory such as /var/run/postgresql.

The password is best supplied by libpq itself, from ~/.pgpass (chmod 600) or
the PGPASSWORD variable, so it never has to appear in a URL, a file in the
repository, or a shell history.  Both psycopg (load_data.py, query_data.py)
and SQLAlchemy's psycopg driver (models.py) go through libpq, so the same
settings serve every part of the project.
"""

from __future__ import annotations

import os
import re

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

DEFAULT_HOST = "localhost"
DEFAULT_PORT = "5432"
DEFAULT_USER = "gradcafe"
DEFAULT_DATABASE = "gradcafe"
CONNECT_TIMEOUT_SECONDS = 10

TABLE_NAME = "applicants"

INVALID_SETTINGS_MESSAGE = (
    "error: the database connection settings are not valid; check DATABASE_URL or "
    "PGHOST/PGPORT/PGUSER/PGDATABASE (see README)"
)

# "postgresql+psycopg://" (SQLAlchemy style) is reduced to the scheme libpq understands.
_DRIVER_SUFFIX = re.compile(r"^(postgres(?:ql)?)\+\w+://", re.IGNORECASE)


def _pg_settings() -> dict[str, str]:
    return {
        "host": os.environ.get("PGHOST", DEFAULT_HOST),
        "port": os.environ.get("PGPORT", DEFAULT_PORT),
        "user": os.environ.get("PGUSER", DEFAULT_USER),
        "dbname": os.environ.get("PGDATABASE", DEFAULT_DATABASE),
    }


def get_database_url() -> str:
    """Connection string for psycopg: DATABASE_URL (URL or key=value), else a key=value DSN from PG* variables."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return _DRIVER_SUFFIX.sub(r"\1://", url, count=1)
    return make_conninfo(**_pg_settings())


def get_sqlalchemy_url():
    """The same connection as a sqlalchemy.engine.URL that selects the psycopg (v3) driver.

    The settings are parsed by psycopg's own libpq-compatible parser and rebuilt
    field by field, so URLs, key=value strings, Unix-socket directories and IPv6
    hosts all work, and extra options such as sslmode are kept.  A non-numeric
    port is passed through unchanged so libpq rejects it when connecting, with
    the same connection error the psycopg scripts report.  Raises
    psycopg.ProgrammingError if the settings cannot be parsed at all.
    """
    from sqlalchemy.engine import URL

    params = dict(conninfo_to_dict(get_database_url()))
    user = params.pop("user", None)
    password = params.pop("password", None)
    host = params.pop("host", None)
    dbname = params.pop("dbname", None)
    port = str(params.pop("port", "") or "")
    if port and not port.isdigit():
        params["port"] = port
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=int(port) if port.isdigit() else None,
        database=dbname,
        query=params,
    )


def describe_target() -> str:
    """``user@host:port/dbname`` for messages; never raises and never shows a password or the raw setting."""
    try:
        params = conninfo_to_dict(get_database_url())
    except (psycopg.ProgrammingError, ValueError):
        return "<unparseable DATABASE_URL>"
    user = params.get("user") or DEFAULT_USER
    host = params.get("host") or DEFAULT_HOST
    port = params.get("port") or DEFAULT_PORT
    dbname = params.get("dbname") or DEFAULT_DATABASE
    return f"{user}@{host}:{port}/{dbname}"
