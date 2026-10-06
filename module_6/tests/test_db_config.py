"""
test_db_config.py - How the app finds PostgreSQL, without ever printing a password.

Every test here only sets environment variables with monkeypatch and reads
back what db_config builds from them; none of them opens a connection.
"""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from db.db_config import describe_target, get_database_url
from worker.etl.models import get_sqlalchemy_url   # SQLAlchemy lives in the worker only

pytestmark = pytest.mark.db          # marks every test in this file


def test_pg_variables_are_used_when_database_url_is_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("PGHOST", "db.internal")
    monkeypatch.setenv("PGPORT", "6543")
    monkeypatch.setenv("PGUSER", "grader")
    monkeypatch.setenv("PGDATABASE", "grades")

    assert conninfo_to_dict(get_database_url()) == {
        "host": "db.internal", "port": "6543", "user": "grader", "dbname": "grades",
    }


def test_sqlalchemy_style_url_is_reduced_to_plain_postgresql(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://grader@db.internal:6543/grades")

    assert get_database_url() == "postgresql://grader@db.internal:6543/grades"


def test_sqlalchemy_url_selects_the_psycopg_driver_and_keeps_options(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://grader@db.internal:6543/grades?sslmode=require")

    url = get_sqlalchemy_url()

    assert url.drivername == "postgresql+psycopg"
    assert (url.username, url.host, url.port, url.database) == ("grader", "db.internal", 6543, "grades")
    assert url.query == {"sslmode": "require"}


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("postgresql://alice:s3cret@db.example:6543/grads", "alice@db.example:6543/grads"),
        ("dbname=only_this", "gradcafe@localhost:5432/only_this"),
        ("dbname='unterminated", "<unparseable DATABASE_URL>"),
    ],
)
def test_describe_target_never_shows_the_password(monkeypatch, setting, expected):
    monkeypatch.setenv("DATABASE_URL", setting)

    assert describe_target() == expected
    assert "s3cret" not in describe_target()