"""
test_db_hardening.py - The database side of security: secrets and privileges.

Three promises are checked here:

1. No credentials live in the code.  The connection comes from the
   environment (DATABASE_URL, or DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD),
   no string in src/ looks like a password, and .env, where the real values
   go, is ignored by git.
2. Each service's database role has the least privilege it needs.
   db_roles.py creates a web role that may only SELECT (applicants and
   analysis_snapshot) and a worker role that may SELECT and INSERT entries
   and UPSERT its two bookkeeping tables; these tests log in as throwaway
   copies of them and try everything else.
3. The services still work as those roles: the worker's insert, watermark
   and snapshot writes, and the web app's page, status and search reads.

Roles belong to the whole PostgreSQL server, not to one database, so each test
uses roles with random names, and the throwaway_roles fixture always drops
them afterwards, even when the test fails.  No test ever touches the default
names gradcafe_web or gradcafe_worker.
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

from db import db_roles, load_data
from db.db_config import describe_target, get_database_url
from db.db_roles import (WEB_ROLE, WORKER_ROLE, RoleError, RoleSpec, build_role_statements,
                         provision_role, provision_roles, role_privileges)
from db.load_data import count_rows, get_watermark, set_watermark
from db.snapshot import load_snapshot, save_snapshot
from web.app import services
from web.app.applicant_search import SearchRequest
from worker.etl.analytics import refresh_snapshot
from worker.etl.ingest import insert_scraped_entries
from worker.etl.models import get_sqlalchemy_url

pytestmark = pytest.mark.db          # marks every test in this file

MODULE = Path(__file__).resolve().parent.parent      # module_6/
SRC = MODULE / "src"

# A least-privilege service role can log in, and that is all it can do on its own...
EXPECTED_ATTRIBUTES = {
    "rolsuper": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolinherit": False,
    "rolreplication": False,
    "rolbypassrls": False,
    "rolcanlogin": True,
    "rolconnlimit": 10,                               # at most 10 connections...
    "rolconfig": ["statement_timeout=30s"],           # ...and no statement runs over 30 s
}
# ...plus exactly these privileges, on exactly these tables.
EXPECTED_WEB = {**EXPECTED_ATTRIBUTES, "table_privileges": {
    "analysis_snapshot": ["SELECT"],
    "applicants": ["SELECT"],                         # nothing at all on ingestion_watermarks
}}
EXPECTED_WORKER = {**EXPECTED_ATTRIBUTES, "table_privileges": {
    "analysis_snapshot": ["INSERT", "SELECT", "UPDATE"],
    "applicants": ["INSERT", "SELECT"],               # never UPDATE or DELETE an entry
    "ingestion_watermarks": ["INSERT", "SELECT", "UPDATE"],
}}


# --------------------------------------------------------------------------- #
#               Settings: the DB_* variables and who wins over whom           #
# --------------------------------------------------------------------------- #

def test_db_variables_describe_the_connection(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_PORT", "6543")
    monkeypatch.setenv("DB_NAME", "grades")
    monkeypatch.setenv("DB_USER", "gradcafe_web")
    password = secrets.token_urlsafe(16)               # a fresh throwaway value, never a real one
    monkeypatch.setenv("DB_PASSWORD", password)

    assert conninfo_to_dict(get_database_url()) == {
        "host": "db.internal", "port": "6543", "dbname": "grades",
        "user": "gradcafe_web", "password": password,  # the password reaches libpq
    }
    assert get_sqlalchemy_url().password == password    # and SQLAlchemy,
    assert password not in str(get_sqlalchemy_url())    # which prints it as ***
    assert describe_target() == "gradcafe_web@db.internal:6543/grades"  # messages never show it


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
# The one exception is the placeholder spelled exactly PASSWORD, in capitals,
# that the docstrings and error messages use to show the form of a URL
# (amqp://USER:PASSWORD@HOST:5672/).
CREDENTIAL = re.compile(r"://[^\s/@:]+:(?!(?-i:PASSWORD)@)[^\s/@]+@|password\s*=\s*\S",
                        re.IGNORECASE)


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
        ("amqp://USER:PASSWORD@HOST:5672/", False),               # the documented placeholder
        ("amqp://gradcafe:password@rabbitmq:5672/", True),        # but a real password "password"
        ("amqp://gradcafe:PASSWORD1@rabbitmq:5672/", True),       # or one that only starts so
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

    assert SRC / "db" / "db_config.py" in files         # the scan really read the project
    assert found == []                                  # and nothing looks like a password


def test_env_example_lists_the_settings_and_env_is_git_ignored():
    example = (MODULE / ".env.example").read_text(encoding="utf-8")
    # The .gitignore rules live at the repository root (module_6/.gitignore was moved there).
    ignored = (MODULE.parent / ".gitignore").read_text(encoding="utf-8").splitlines()

    for name in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
                 "WEB_DB_USER", "WEB_DB_PASSWORD", "WORKER_DB_USER", "WORKER_DB_PASSWORD",
                 "POSTGRES_PASSWORD", "RABBITMQ_DEFAULT_USER", "RABBITMQ_DEFAULT_PASS"):
        assert f"export {name}=" in example
    secrets_shown = re.findall(r"(?:PASSWORD|_PASS)=(\S+)", example)
    assert set(secrets_shown) == {"change-me"}          # placeholders only
    assert ".env" in ignored                            # the real values never reach git
    assert not (MODULE / ".gitignore").exists()


# --------------------------------------------------------------------------- #
#             Throwaway service roles, always dropped afterwards              #
# --------------------------------------------------------------------------- #

class ServiceRole(NamedTuple):
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
def throwaway_roles(db_conn):
    """Hands out role names no real role has (random suffix): throwaway_roles("web").
    After the test, pass or fail, every role that got one of those names is dropped."""
    names = []

    def new_name(service: str) -> str:
        names.append(f"gradcafe_{service}_test_{secrets.token_hex(4)}")
        return names[-1]

    yield new_name
    for name in names:
        drop_role(db_conn, name)


def provisioned(db_conn, spec: RoleSpec, name: str, database_url: str) -> ServiceRole:
    """Provision *name* exactly the way db_roles.py provisions the *spec* role.

    The login really checks the random password: the Docker database and the
    CI service container both use scram-sha-256 over TCP.  (In CI, PGPASSWORD
    gives libpq the owner's password; the password in this URL overrides it.)
    """
    password = secrets.token_urlsafe(24)
    provision_role(db_conn, spec, name, password)
    return ServiceRole(name, role_url(database_url, name, password))


@pytest.fixture
def web_role(db_conn, throwaway_roles, database_url) -> ServiceRole:
    """A throwaway copy of the web app's read-only role."""
    return provisioned(db_conn, WEB_ROLE, throwaway_roles("web"), database_url)


@pytest.fixture
def worker_role(db_conn, throwaway_roles, database_url) -> ServiceRole:
    """A throwaway copy of the worker's role."""
    return provisioned(db_conn, WORKER_ROLE, throwaway_roles("worker"), database_url)


# --------------------------------------------------------------------------- #
#                      Building and provisioning the roles                    #
# --------------------------------------------------------------------------- #

def test_role_statements_grant_exactly_what_each_service_needs():
    web = [s.as_string() for s in build_role_statements(WEB_ROLE, "gradcafe_web", "it's-secret",
                                                         "gradcafe", exists=False)]
    worker = [s.as_string() for s in build_role_statements(WORKER_ROLE, "gradcafe_worker", "pw",
                                                            "gradcafe", exists=False)]

    assert web[0].startswith('CREATE ROLE "gradcafe_web" WITH LOGIN NOSUPERUSER NOCREATEDB')
    assert web[0].endswith("PASSWORD 'it''s-secret'")      # the quote stays inside the literal
    assert "CONNECTION LIMIT 10" in web[0]
    assert web[1:] == [
        """ALTER ROLE "gradcafe_web" SET statement_timeout = '30s'""",
        'REVOKE ALL PRIVILEGES ON TABLE "applicants", "ingestion_watermarks", "analysis_snapshot" '
        'FROM "gradcafe_web"',
        'GRANT CONNECT ON DATABASE "gradcafe" TO "gradcafe_web"',
        'GRANT USAGE ON SCHEMA "public" TO "gradcafe_web"',
        'GRANT SELECT ON TABLE "applicants" TO "gradcafe_web"',
        'GRANT SELECT ON TABLE "analysis_snapshot" TO "gradcafe_web"',
    ]
    assert worker[5:] == [
        'GRANT SELECT, INSERT ON TABLE "applicants" TO "gradcafe_worker"',
        'GRANT SELECT, INSERT, UPDATE ON TABLE "ingestion_watermarks" TO "gradcafe_worker"',
        'GRANT SELECT, INSERT, UPDATE ON TABLE "analysis_snapshot" TO "gradcafe_worker"',
    ]
    # A role that already exists gets ALTER ROLE with the same options instead.
    again = build_role_statements(WEB_ROLE, "gradcafe_web", "x", "gradcafe", exists=True)
    assert again[0].as_string().startswith('ALTER ROLE "gradcafe_web" WITH LOGIN NOSUPERUSER')


def test_a_hostile_database_name_stays_one_quoted_identifier():
    # Snyk Code reports the database name (read back from the server) reaching execute() as
    # SQL injection.  sql.Identifier double-quotes it and doubles any quote inside, so even
    # this name stays a single identifier and the DROP is never run as SQL.
    statements = build_role_statements(WEB_ROLE, "gradcafe_web", "pw",
                                       'x"; DROP TABLE applicants; --', exists=False)

    assert statements[3].as_string() == (
        'GRANT CONNECT ON DATABASE "x""; DROP TABLE applicants; --" TO "gradcafe_web"'
    )


@pytest.mark.parametrize(
    ("role", "password"),
    [
        ("Gradcafe_Web", "pw"),                         # upper case
        ("1web", "pw"),                                 # starts with a digit
        ("web; DROP ROLE gradcafe", "pw"),              # an injection attempt
        ("a" * 64, "pw"),                               # longer than PostgreSQL's 63 characters
        ("gradcafe_web\n", "pw"),                       # a sneaky trailing newline
        ("gradcafe_web", ""),                           # no password
        ("gradcafe_web", "   "),                        # a blank password
    ],
)
def test_unsafe_names_and_blank_passwords_are_refused(role, password):
    with pytest.raises(RoleError):
        build_role_statements(WEB_ROLE, role, password, "gradcafe", exists=False)


@pytest.mark.parametrize("privileges", [("SELECT", "DELETE"), ("TRUNCATE",), ("SELECT; DROP",), ()])
def test_only_select_insert_and_update_can_ever_be_granted(privileges):
    spec = RoleSpec("web", "gradcafe_web", "WEB_DB_USER", "WEB_DB_PASSWORD",
                    grants=(("applicants", privileges),))

    with pytest.raises(RoleError, match="only SELECT, INSERT, UPDATE may be granted"):
        build_role_statements(spec, "gradcafe_web", "pw", "gradcafe", exists=False)


def test_each_provisioned_role_has_exactly_its_privileges(db_conn, web_role, worker_role):
    assert role_privileges(db_conn, web_role.name) == EXPECTED_WEB
    assert role_privileges(db_conn, worker_role.name) == EXPECTED_WORKER


def test_provisioning_again_resets_the_role(db_conn, web_role, database_url):
    # Widen the role by hand, the way a careless administrator might...
    db_conn.execute(sql.SQL("ALTER ROLE {} CREATEDB").format(sql.Identifier(web_role.name)))
    db_conn.execute(sql.SQL("GRANT INSERT, DELETE ON applicants, ingestion_watermarks TO {}")
                    .format(sql.Identifier(web_role.name)))
    new_password = secrets.token_urlsafe(24)

    provision_role(db_conn, WEB_ROLE, web_role.name, new_password)    # ...then provision again

    assert role_privileges(db_conn, web_role.name) == EXPECTED_WEB
    with psycopg.connect(role_url(database_url, web_role.name, new_password)) as conn:
        assert conn.execute("SELECT current_user LIMIT 1").fetchone() == (web_role.name,)


def test_the_administrator_itself_is_never_changed(db_conn):
    # force_rollback: even if the guard were missing, nothing done here would stay.
    with pytest.raises(RoleError, match="refusing"), db_conn.transaction(force_rollback=True):
        provision_role(db_conn, WEB_ROLE, db_conn.info.user, "not-used")


def test_the_web_and_worker_roles_must_have_different_names(db_conn, throwaway_roles):
    shared = throwaway_roles("shared")

    with pytest.raises(RoleError, match="different names"):
        provision_roles(db_conn, [(WEB_ROLE, shared, "pw-1"), (WORKER_ROLE, shared, "pw-2")])

    assert role_privileges(db_conn, shared) == {"table_privileges": {}}     # nothing was created


# --------------------------------------------------------------------------- #
#                  Logged in as the web role: reading only                    #
# --------------------------------------------------------------------------- #

def test_the_web_role_can_read_the_entries_and_the_snapshot(db_conn, web_role):
    db_conn.execute("INSERT INTO applicants (p_id, program) VALUES (1, 'Physics, MIT')")
    with db_conn.transaction():
        save_snapshot(db_conn, {"summary": {"total_entries": 1, "newest_entry": None}, "answers": []})

    with psycopg.connect(web_role.url) as conn:
        rows = conn.execute("SELECT p_id, program FROM applicants ORDER BY p_id LIMIT 10").fetchall()
        analysis = load_snapshot(conn)
        timeout = conn.execute("SHOW statement_timeout").fetchone()[0]

    assert rows == [(1, "Physics, MIT")]
    assert analysis["summary"]["total_entries"] == 1
    assert timeout == "30s"                             # the role's limit applies to every login


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO applicants (p_id, program) VALUES (2, 'Chemistry, MIT')",
        "UPDATE applicants SET gpa = 4.0",
        "DELETE FROM applicants",
        "TRUNCATE applicants",
        "DROP TABLE applicants",
        "ALTER TABLE applicants ADD COLUMN hacked TEXT",
        "CREATE TABLE hacked (id INTEGER)",
        "CREATE ROLE hacked LOGIN SUPERUSER",
        "SELECT last_seen FROM ingestion_watermarks LIMIT 1",
        "INSERT INTO ingestion_watermarks (source, last_seen) VALUES ('gradcafe', '1')",
        "UPDATE analysis_snapshot SET payload = '{}'",
        "DELETE FROM analysis_snapshot",
    ],
)
def test_the_web_role_cannot_write_or_destroy_anything(db_conn, web_role, statement):
    db_conn.execute("INSERT INTO applicants (p_id, program) VALUES (1, 'Physics, MIT')")

    # No autocommit: if a statement ever did succeed, the failed test rolls it back.
    with psycopg.connect(web_role.url) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)

    assert count_rows(db_conn) == 1                     # the row is still there


# --------------------------------------------------------------------------- #
#        Logged in as the worker role: adding entries, upserting its rows     #
# --------------------------------------------------------------------------- #

def test_the_worker_role_can_insert_entries_and_upsert_its_bookkeeping(db_conn, worker_role):
    payload = {"summary": {"total_entries": 1, "newest_entry": None}, "answers": []}
    with psycopg.connect(worker_role.url) as conn:
        conn.execute("INSERT INTO applicants (p_id, program) VALUES (%s, %s)", [1, "Physics, MIT"])
        set_watermark(conn, "1")
        set_watermark(conn, "2")                        # INSERT ... ON CONFLICT DO UPDATE
        save_snapshot(conn, payload)
        save_snapshot(conn, payload)
        watermark = get_watermark(conn)

    assert watermark == "2"
    assert count_rows(db_conn) == 1                     # the insert was committed
    assert db_conn.execute("SELECT count(*) FROM analysis_snapshot").fetchone() == (1,)


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
        "DELETE FROM ingestion_watermarks",
        "TRUNCATE analysis_snapshot",
        "DROP TABLE analysis_snapshot",
    ],
)
def test_the_worker_role_cannot_change_or_destroy_anything(db_conn, worker_role, statement):
    db_conn.execute("INSERT INTO applicants (p_id, program) VALUES (1, 'Physics, MIT')")

    with psycopg.connect(worker_role.url) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)

    assert count_rows(db_conn) == 1


def test_each_service_works_as_its_own_role(db_conn, web_role, worker_role, raw_entries, monkeypatch):
    # The worker stores a pull and refreshes the snapshot in one transaction...
    with psycopg.connect(worker_role.url) as conn:
        assert conn.info.user == worker_role.name      # (really: not as the owner)
        with conn.transaction():
            inserted = insert_scraped_entries(conn, raw_entries)   # clean + temporary table + INSERT
            set_watermark(conn, "9000003")
            refresh_snapshot(conn)
        inserted_again = insert_scraped_entries(conn, raw_entries)  # ON CONFLICT DO NOTHING

    # ...and the web app reads the page, the status and the search API with its own role.
    monkeypatch.setenv("DATABASE_URL", web_role.url)
    with load_data.connect() as conn:
        assert conn.info.user == web_role.name
    analysis = services.default_query_fn()
    status = services.default_status_fn()
    rows = services.default_search_fn(SearchRequest(filters={"term": "Fall 2027"}, sort="p_id"))

    assert (inserted, inserted_again) == (3, 0)
    assert analysis["summary"]["total_entries"] == 3
    assert status["total_entries"] == 3 and status["computed_at"] == analysis["computed_at"]
    assert [row["p_id"] for row in rows] == [9_000_003, 9_000_002, 9_000_001]
    assert count_rows(db_conn) == 3


# --------------------------------------------------------------------------- #
#                   The command line: python -m db.db_roles                   #
# --------------------------------------------------------------------------- #

def test_the_command_line_needs_the_passwords(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["db_roles.py"])  # no *_DB_PASSWORD is set (see conftest)

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("db.db_roles", run_name="__main__")   # same as: python -m db.db_roles

    assert stopped.value.code == 1
    assert "WEB_DB_PASSWORD" in capsys.readouterr().err


def test_the_command_line_never_prints_the_passwords(db_conn, throwaway_roles, monkeypatch, capsys):
    names = {"WEB_DB_USER": throwaway_roles("web"), "WORKER_DB_USER": throwaway_roles("worker")}
    passwords = {"WEB_DB_PASSWORD": secrets.token_urlsafe(24),
                 "WORKER_DB_PASSWORD": secrets.token_urlsafe(24)}
    for variable, value in {**names, **passwords}.items():
        monkeypatch.setenv(variable, value)

    assert db_roles.main([]) == 0

    printed = capsys.readouterr()
    assert f"Role {names['WEB_DB_USER']} is ready" in printed.out
    assert f"Role {names['WORKER_DB_USER']} is ready" in printed.out
    assert "privileges on ingestion_watermarks: none" in printed.out          # the web role's
    assert "privileges on ingestion_watermarks: INSERT, SELECT, UPDATE" in printed.out
    for password in passwords.values():
        assert password not in printed.out + printed.err
    assert role_privileges(db_conn, names["WEB_DB_USER"]) == EXPECTED_WEB
    assert role_privileges(db_conn, names["WORKER_DB_USER"]) == EXPECTED_WORKER


def test_the_command_line_refuses_one_name_for_both_roles(db_conn, throwaway_roles, monkeypatch, capsys):
    shared = throwaway_roles("shared")
    for variable in ("WEB_DB_USER", "WORKER_DB_USER"):
        monkeypatch.setenv(variable, shared)
    for variable in ("WEB_DB_PASSWORD", "WORKER_DB_PASSWORD"):
        monkeypatch.setenv(variable, secrets.token_urlsafe(24))

    assert db_roles.main([]) == 1
    assert "different names" in capsys.readouterr().err
    assert role_privileges(db_conn, shared) == {"table_privileges": {}}
