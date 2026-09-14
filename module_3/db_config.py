"""
db_config.py - One place that knows how to reach PostgreSQL.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

No credentials live in this repository.  The connection is described by
environment variables, in this order of preference:

  1. DATABASE_URL, e.g.  postgresql://gradcafe@localhost:5432/gradcafe
  2. The standard libpq variables PGHOST, PGPORT, PGUSER, PGDATABASE
     (defaults: localhost, 5432, gradcafe, gradcafe).

The password is best supplied by libpq itself, from ~/.pgpass (chmod 600) or
the PGPASSWORD variable, so it never has to appear in a URL, a file in the
repository, or a shell history.  Both psycopg (load_data.py, query_data.py)
and SQLAlchemy's psycopg driver (models.py) go through libpq, so the same
settings serve every part of the project.
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlsplit, urlunsplit

DEFAULT_HOST = "localhost"
DEFAULT_PORT = "5432"
DEFAULT_USER = "gradcafe"
DEFAULT_DATABASE = "gradcafe"

TABLE_NAME = "applicants"


def get_database_url() -> str:
    """Return a libpq connection URL (postgresql://...) built from the environment."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    host = os.environ.get("PGHOST", DEFAULT_HOST)
    port = os.environ.get("PGPORT", DEFAULT_PORT)
    user = os.environ.get("PGUSER", DEFAULT_USER)
    database = os.environ.get("PGDATABASE", DEFAULT_DATABASE)
    return f"postgresql://{quote(user)}@{host}:{port}/{quote(database)}"


def get_sqlalchemy_url() -> str:
    """The same connection as a SQLAlchemy URL that selects the psycopg (v3) driver."""
    parts = urlsplit(get_database_url())
    scheme = parts.scheme.split("+", 1)[0]
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+psycopg"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def describe_target() -> str:
    """Connection target without any password, safe for log and error messages."""
    parts = urlsplit(get_database_url())
    host = parts.hostname or DEFAULT_HOST
    port = parts.port or DEFAULT_PORT
    user = parts.username or DEFAULT_USER
    return f"{user}@{host}:{port}{parts.path or '/' + DEFAULT_DATABASE}"
