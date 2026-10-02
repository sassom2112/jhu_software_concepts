"""
test_db_hardening.py - The database side of security: secrets and privileges.

Three promises are checked here:

1. No credentials live in the code.  The connection comes from the
   environment (DATABASE_URL, or DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD),
   no string in src/ looks like a password, and .env, where the real values
   go, is ignored by git.
2. The web app's database role has the least privilege it needs.
   db_roles.py creates a role that may SELECT and INSERT on applicants and
   nothing else; these tests log in as a throwaway copy of it and try
   everything else.
3. The app still works as that role: the real "Pull Data" loader inserts and
   the real search reads.

Roles belong to the whole PostgreSQL server, not to one database, so each test
uses a role with a random name, and the throwaway_role fixture always drops it
afterwards, even when the test fails.
"""

from __future__ import annotations

import ast
import re
import runpy
import secrets
import sys
from pathlib import Path
from typing import NamedTuple

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import db_roles
import load_data
from applicant_search import SearchRequest
from db_config import describe_target, get_database_url, get_sqlalchemy_url
from db_roles import RoleError, build_role_statements, provision_app_role, role_privileges
from load_data import count_rows
from webapp import services

pytestmark = pytest.mark.db          # marks every test in this file

MODULE = Path(__file__).resolve().parent.parent      # module_5/
SRC = MODULE / "src"

# A least-privilege app role: it can log in, read and add rows, and that is all.
EXPECTED_PRIVILEGES = {
    "rolsuper": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolinherit": False,
    "rolreplication": False,
    "rolbypassrls": False,
    "rolcanlogin": True,
    "rolconnlimit": 10,                               # at most 10 connections...
    "rolconfig": ["statement_timeout=30s"],           # ...and no statement runs over 30 s
    "table_privileges": ["INSERT", "SELECT"],
}


# --------------------------------------------------------------------------- #
#               Settings: the DB_* variables and who wins over whom           #
# --------------------------------------------------------------------------- #

def test_db_variables_describe_the_connection(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_PORT", "6543")
    monkeypatch.setenv("DB_NAME", "grades")
    monkeypatch.setenv("DB_USER", "gradcafe_app")
    password = secrets.token_urlsafe(16)               # a fresh throwaway value, never a real one
    monkeypatch.setenv("DB_PASSWORD", password)

    assert conninfo_to_dict(get_database_url()) == {
        "host": "db.internal", "port": "6543", "dbname": "grades",
        "user": "gradcafe_app", "password": password,  # the password reaches libpq
    }
    assert get_sqlalchemy_url().password == password    # and SQLAlchemy,
    assert password not in str(get_sqlalchemy_url())    # which prints it as ***
    assert describe_target() == "gradcafe_app@db.internal:6543/grades"  # messages never show it


def test_database_url_beats_db_variables_which_beat_pg_variables(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PGPORT", raising=False)
    monkeypatch.delenv("PGDATABASE", raising=False)
    monkeypatch.setenv("PGHOST", "pg-host")
    monkeypatch.setenv("PGUSER", "pg_user")
    monkeypatch.setenv("DB_HOST", "db-host")
    monkeypatch.setenv("DB_USER", "")                   # blank counts as not set

    assert conninfo_to_dict(get_database_url()) == {
        "host": "db-host",                              # DB_HOST beats PGHOST
        "user": "pg_user",                              # PGUSER, since DB_USER is blank
        "port": "5432", "dbname": "gradcafe",           # the defaults fill in the rest
    }

    monkeypatch.setenv("DATABASE_URL", "postgresql://url_user@url-host/url_db")
    monkeypatch.setenv("DB_PASSWORD", "ignored")

    assert conninfo_to_dict(get_database_url()) == {    # DATABASE_URL beats everything
        "user": "url_user", "host": "url-host", "dbname": "url_db",
    }


# --------------------------------------------------------------------------- #
#             No credentials in the code, and the real .env stays private     #
# --------------------------------------------------------------------------- #

# The two shapes a hard-coded credential takes: a URL with user:password@ in
# it, or password=<something> (libpq's key=value spelling, or a shell line).
CREDENTIAL = re.compile(r"://[^\s/@:]+:[^\s/@]+@|password\s*=\s*\S", re.IGNORECASE)


def string_constants(path: Path):
    """Every string literal in a Python file (docstrings and f-string pieces too), with its line."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


@pytest.mark.parametrize(
    ("text", "is_credential"),
    [
        ("postgresql://alice:s3cret@db.example:5432/grads", True),
        ("host=localhost password=s3cret", True),
        ("PGPASSWORD=s3cret", True),
        ("postgresql://gradcafe@localhost:5432/gradcafe", False),  # a user, but no password
        ("https://www.thegradcafe.com/survey?page=2", False),
    ],
)
def test_the_credential_pattern_spots_passwords(text, is_credential):
    assert bool(CREDENTIAL.search(text)) is is_credential


def test_no_credentials_are_written_into_the_source():
    files = sorted(SRC.rglob("*.py"))
    found = [
        f"{path.relative_to(SRC)}:{line}: {text!r}"
        for path in files
        for line, text in string_constants(path)
        if CREDENTIAL.search(text)
    ]

    assert SRC / "db_config.py" in files                # the scan really read the project
    assert found == []                                  # and nothing looks like a password


def test_env_example_lists_the_settings_and_env_is_git_ignored():
    example = (MODULE / ".env.example").read_text(encoding="utf-8")
    ignored = (MODULE / ".gitignore").read_text(encoding="utf-8").splitlines()

    for name in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "APP_DB_PASSWORD"):
        assert f"export {name}=" in example
    assert set(re.findall(r"PASSWORD=(\S+)", example)) == {"change-me"}   # placeholders only
    assert ".env" in ignored                            # the real values never reach git


# --------------------------------------------------------------------------- #
#                 A throwaway app role, always dropped afterwards             #
# --------------------------------------------------------------------------- #

class AppRole(NamedTuple):
    """A provisioned throwaway role: its name, and a connection string that logs in as it."""

    name: str
    url: str


def role_url(database_url: str, role: str, password: str) -> str:
    """The test database's connection string, with the user and password replaced."""
    params = conninfo_to_dict(database_url)
    params.update(user=role, password=password)
    return make_conninfo(**params)


def drop_role(conn, role: str) -> None:
    """DROP OWNED BY + DROP ROLE, as the owner.  A role that was never created is skipped."""
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s LIMIT 1", [role]).fetchone():
        conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture
def throwaway_role(db_conn):
    """A role name no real role has (random suffix).  After the test, pass or fail,
    whatever role got that name is dropped again."""
    role = f"gradcafe_app_test_{secrets.token_hex(4)}"
    yield role
    drop_role(db_conn, role)


@pytest.fixture
def app_role(db_conn, throwaway_role, database_url) -> AppRole:
    """The throwaway role, provisioned exactly the way db_roles.py provisions gradcafe_app.

    The login really checks the random password: the Docker database and the
    CI service container both use scram-sha-256 over TCP.  (In CI, PGPASSWORD
    gives libpq the owner's password; the password in this URL overrides it.)
    """
    password = secrets.token_urlsafe(24)
    provision_app_role(db_conn, throwaway_role, password)
    return AppRole(throwaway_role, role_url(database_url, throwaway_role, password))


# --------------------------------------------------------------------------- #
#                      Building and provisioning the role                     #
# --------------------------------------------------------------------------- #

def test_role_statements_grant_select_and_insert_only():
    statements = build_role_statements("gradcafe_app", "it's-secret", "gradcafe", exists=False)
    texts = [statement.as_string() for statement in statements]

    assert texts[0].startswith('CREATE ROLE "gradcafe_app" WITH LOGIN NOSUPERUSER NOCREATEDB')
    assert texts[0].endswith("PASSWORD 'it''s-secret'")    # the quote stays inside the literal
    assert "CONNECTION LIMIT 10" in texts[0]
    assert texts[1:] == [
        """ALTER ROLE "gradcafe_app" SET statement_timeout = '30s'""",
        'REVOKE ALL PRIVILEGES ON TABLE "applicants" FROM "gradcafe_app"',
        'GRANT CONNECT ON DATABASE "gradcafe" TO "gradcafe_app"',
        'GRANT USAGE ON SCHEMA "public" TO "gradcafe_app"',
        'GRANT SELECT, INSERT ON TABLE "applicants" TO "gradcafe_app"',
    ]
    # A role that already exists gets ALTER ROLE with the same options instead.
    again = build_role_statements("gradcafe_app", "x", "gradcafe", exists=True)
    assert again[0].as_string().startswith('ALTER ROLE "gradcafe_app" WITH LOGIN NOSUPERUSER')

def test_a_hostile_database_name_stays_one_quoted_identifier():
    # Snyk Code reports the database name (read back from the server) reaching execute() as
    # SQL injection.  sql.Identifier double-quotes it and doubles any quote inside, so even
    # this name stays a single identifier and the DROP is never run as SQL.
    statements = build_role_statements("gradcafe_app", "pw", 'x"; DROP TABLE applicants; --',
                                       exists=False)

    assert statements[3].as_string() == (
        'GRANT CONNECT ON DATABASE "x""; DROP TABLE applicants; --" TO "gradcafe_app"'
    )

@pytest.mark.parametrize(
    ("role", "password"),
    [
        ("Gradcafe_App", "pw"),                         # upper case
        ("1app", "pw"),                                 # starts with a digit
        ("app; DROP ROLE gradcafe", "pw"),              # an injection attempt
        ("a" * 64, "pw"),                               # longer than PostgreSQL's 63 characters
        ("gradcafe_app\n", "pw"),                       # a sneaky trailing newline
        ("gradcafe_app", ""),                           # no password
        ("gradcafe_app", "   "),                        # a blank password
    ],
)
def test_unsafe_names_and_blank_passwords_are_refused(role, password):
    with pytest.raises(RoleError):
        build_role_statements(role, password, "gradcafe", exists=False)


def test_the_provisioned_role_can_only_log_in_select_and_insert(db_conn, app_role):
    assert role_privileges(db_conn, app_role.name) == EXPECTED_PRIVILEGES


def test_provisioning_again_resets_the_role(db_conn, app_role, database_url):
    # Widen the role by hand, the way a careless administrator might...
    db_conn.execute(sql.SQL("ALTER ROLE {} CREATEDB").format(sql.Identifier(app_role.name)))
    db_conn.execute(sql.SQL("GRANT UPDATE, DELETE ON applicants TO {}")
                    .format(sql.Identifier(app_role.name)))
    new_password = secrets.token_urlsafe(24)

    provision_app_role(db_conn, app_role.name, new_password)    # ...then provision again

    assert role_privileges(db_conn, app_role.name) == EXPECTED_PRIVILEGES
    with psycopg.connect(role_url(database_url, app_role.name, new_password)) as conn:
        assert conn.execute("SELECT current_user LIMIT 1").fetchone() == (app_role.name,)


def test_the_administrator_itself_is_never_changed(db_conn):
    # force_rollback: even if the guard were missing, nothing done here would stay.
    with pytest.raises(RoleError, match="refusing"), db_conn.transaction(force_rollback=True):
        provision_app_role(db_conn, db_conn.info.user, "not-used")


# --------------------------------------------------------------------------- #
#                Logged in as the role: reading and adding only               #
# --------------------------------------------------------------------------- #

def test_the_app_role_can_select_and_insert(db_conn, app_role):
    with psycopg.connect(app_role.url) as conn:
        conn.execute("INSERT INTO applicants (p_id, program) VALUES (%s, %s)", [1, "Physics, MIT"])
        rows = conn.execute("SELECT p_id, program FROM applicants ORDER BY p_id LIMIT 10").fetchall()
        timeout = conn.execute("SHOW statement_timeout").fetchone()[0]

    assert rows == [(1, "Physics, MIT")]
    assert timeout == "30s"                             # the role's limit applies to every login
    assert count_rows(db_conn) == 1                     # the insert was committed


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE applicants SET gpa = 4.0",
        "DELETE FROM applicants",
        "TRUNCATE applicants",
        "DROP TABLE applicants",
        "ALTER TABLE applicants ADD COLUMN hacked TEXT",
        "CREATE TABLE hacked (id INTEGER)",
        "CREATE ROLE hacked LOGIN SUPERUSER",
    ],
)
def test_the_app_role_cannot_change_or_destroy_anything(db_conn, app_role, statement):
    db_conn.execute("INSERT INTO applicants (p_id, program) VALUES (1, 'Physics, MIT')")

    # No autocommit: if a statement ever did succeed, the failed test rolls it back.
    with psycopg.connect(app_role.url) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)

    assert count_rows(db_conn) == 1                     # the row is still there


def test_pull_data_and_search_work_as_the_app_role(db_conn, app_role, raw_entries, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", app_role.url)    # the app now logs in as the role
    with load_data.connect() as conn:
        assert conn.info.user == app_role.name          # (really: not as the owner)

    inserted = services.default_load_fn(raw_entries)    # clean + temporary staging table + INSERT
    inserted_again = services.default_load_fn(raw_entries)   # ON CONFLICT DO NOTHING: no UPDATE needed
    rows = services.default_search_fn(SearchRequest(filters={"term": "Fall 2027"}, sort="p_id"))

    assert (inserted, inserted_again) == (3, 0)
    assert [row["p_id"] for row in rows] == [9_000_003, 9_000_002, 9_000_001]
    assert count_rows(db_conn) == 3


# --------------------------------------------------------------------------- #
#                   The command line: python src/db_roles.py                  #
# --------------------------------------------------------------------------- #

def test_the_command_line_needs_a_password(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["db_roles.py"])  # APP_DB_PASSWORD is unset (see conftest)

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("db_roles", run_name="__main__")   # same as: python db_roles.py

    assert stopped.value.code == 1
    assert "APP_DB_PASSWORD" in capsys.readouterr().err


def test_the_command_line_never_prints_the_password(db_conn, throwaway_role, monkeypatch, capsys):
    password = secrets.token_urlsafe(24)
    monkeypatch.setenv("APP_DB_USER", throwaway_role)
    monkeypatch.setenv("APP_DB_PASSWORD", password)

    assert db_roles.main([]) == 0

    printed = capsys.readouterr()
    assert f"Role {throwaway_role} is ready" in printed.out
    assert "privileges on applicants: INSERT, SELECT" in printed.out
    assert password not in printed.out + printed.err
    assert role_privileges(db_conn, throwaway_role) == EXPECTED_PRIVILEGES
