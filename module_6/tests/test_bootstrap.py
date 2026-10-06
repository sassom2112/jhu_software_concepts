"""
test_bootstrap.py - ``python -m worker.bootstrap``: the database initialization script.

The init container runs it once, as the table owner, before the web app and the
worker start: it creates the three tables, provisions the web and worker roles,
loads the applicant data and computes the first analysis snapshot, all in one
transaction.  These tests run it against the *_test database from nothing
(the tables are dropped first; the next test's db_conn fixture recreates
them), run it again to show it is harmless, and log in as the roles it made.

The roles get throwaway names (WEB_DB_USER / WORKER_DB_USER are always set
here), never the defaults gradcafe_web and gradcafe_worker, and are dropped
after every test.
"""

from __future__ import annotations

import runpy
import secrets
import sys

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import conninfo_to_dict

from db import load_data
from db.jsonio import save_data
from db.db_roles import role_privileges
from db.load_data import CREATE_TABLE_STATEMENT, count_rows, create_schema, get_watermark
from db.snapshot import load_snapshot, snapshot_status
from worker import bootstrap
from test_db_hardening import EXPECTED_WEB, EXPECTED_WORKER, drop_role, role_url
from test_query_tools import sample_records

pytestmark = pytest.mark.db

TABLES = ("applicants", "ingestion_watermarks", "analysis_snapshot")


@pytest.fixture
def empty_database(db_conn):
    """The test database with none of the three tables: what a brand-new volume looks like."""
    db_conn.execute("DROP TABLE IF EXISTS applicants, ingestion_watermarks, analysis_snapshot")
    return db_conn


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A data folder holding applicant_data.json with 30 synthetic applicants."""
    monkeypatch.setattr(load_data, "DATA_DIR", tmp_path)
    load_data_file(tmp_path, sample_records(30))
    return tmp_path


def load_data_file(folder, records, name="applicant_data.json") -> None:
    """Write *records* as the JSON file bootstrap reads."""
    save_data(records, folder / name)


@pytest.fixture
def service_roles(db_conn, monkeypatch, database_url):
    """WEB_DB_* and WORKER_DB_* set to throwaway names and random passwords.

    Returns {"web": url, "worker": url}: connection strings that log in as each role.
    Both roles are dropped after the test, pass or fail.
    """
    names = {"web": f"gradcafe_web_test_{secrets.token_hex(4)}",
             "worker": f"gradcafe_worker_test_{secrets.token_hex(4)}"}
    passwords = {service: secrets.token_urlsafe(24) for service in names}
    for service in names:
        monkeypatch.setenv(f"{service.upper()}_DB_USER", names[service])
        monkeypatch.setenv(f"{service.upper()}_DB_PASSWORD", passwords[service])
    yield {service: role_url(database_url, names[service], passwords[service]) for service in names}
    for name in names.values():
        drop_role(db_conn, name)


def role_name(url: str) -> str:
    return conninfo_to_dict(url)["user"]


def computed_at(conn):
    return snapshot_status(conn)["computed_at"]


# --------------------------------------------------------------------------- #
#                       From nothing to a working database                    #
# --------------------------------------------------------------------------- #

def test_bootstrap_creates_tables_roles_data_and_the_first_snapshot(empty_database, data_dir,
                                                                     service_roles, capsys):
    assert bootstrap.main([]) == 0

    conn = empty_database
    for table in TABLES:
        assert load_data.table_exists(conn, table)
    assert count_rows(conn) == 30
    assert load_snapshot(conn)["summary"]["total_entries"] == 30
    assert get_watermark(conn) is None                   # no pull has run yet
    assert role_privileges(conn, role_name(service_roles["web"])) == EXPECTED_WEB
    assert role_privileges(conn, role_name(service_roles["worker"])) == EXPECTED_WORKER
    printed = capsys.readouterr()
    assert "inserted 30 new rows" in printed.out
    assert "Analysis snapshot computed" in printed.out
    for url in service_roles.values():                   # the passwords are never printed
        assert conninfo_to_dict(url)["password"] not in printed.out + printed.err


def test_the_watermark_table_is_exactly_the_one_the_assignment_specifies(empty_database, data_dir,
                                                                         service_roles):
    bootstrap.main([])

    columns = empty_database.execute(
        "SELECT column_name, data_type, column_default FROM information_schema.columns "
        "WHERE table_name = 'ingestion_watermarks' ORDER BY ordinal_position"
    ).fetchall()
    primary_key = empty_database.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'ingestion_watermarks'::regclass AND i.indisprimary"
    ).fetchall()

    assert columns == [
        ("source", "text", None),
        ("last_seen", "text", None),
        ("updated_at", "timestamp with time zone", "now()"),
    ]
    assert primary_key == [("source",)]


def test_running_it_again_changes_nothing(empty_database, data_dir, service_roles, capsys):
    bootstrap.main([])
    first = computed_at(empty_database)

    assert bootstrap.main([]) == 0

    assert count_rows(empty_database) == 30               # no duplicates
    assert computed_at(empty_database) == first           # the current snapshot was kept
    assert role_privileges(empty_database, role_name(service_roles["web"])) == EXPECTED_WEB
    printed = capsys.readouterr().out
    assert "inserted 0 new rows; 30 were already present" in printed
    assert "Analysis snapshot already up to date; left unchanged" in printed


def test_new_rows_in_the_data_file_refresh_the_snapshot(empty_database, data_dir, service_roles):
    bootstrap.main([])
    first = computed_at(empty_database)
    load_data_file(data_dir, sample_records(40))           # ten more applicants

    assert bootstrap.main([]) == 0

    assert count_rows(empty_database) == 40
    assert snapshot_status(empty_database)["total_entries"] == 40
    assert computed_at(empty_database) > first


def test_a_missing_snapshot_is_computed_even_when_no_rows_are_new(empty_database, data_dir,
                                                                  service_roles):
    bootstrap.main([])
    empty_database.execute("TRUNCATE analysis_snapshot")

    assert bootstrap.main([]) == 0

    assert snapshot_status(empty_database)["total_entries"] == 30


# --------------------------------------------------------------------------- #
#                   Logged in as the roles bootstrap provisioned               #
# --------------------------------------------------------------------------- #

def test_the_web_role_cannot_insert_and_the_worker_role_cannot_delete_or_drop(empty_database, data_dir,
                                                                              service_roles):
    bootstrap.main([])

    with psycopg.connect(service_roles["web"]) as web:
        assert web.execute("SELECT count(*) FROM applicants LIMIT 1").fetchone() == (30,)
        with pytest.raises(errors.InsufficientPrivilege):
            web.execute("INSERT INTO applicants (p_id) VALUES (1)")
    for statement in ("DELETE FROM applicants", "DROP TABLE applicants",
                      "DROP TABLE ingestion_watermarks", "TRUNCATE analysis_snapshot"):
        with psycopg.connect(service_roles["worker"]) as worker:
            with pytest.raises(errors.InsufficientPrivilege):
                worker.execute(statement)

    assert count_rows(empty_database) == 30


def test_neither_service_role_owns_anything(empty_database, data_dir, service_roles):
    bootstrap.main([])

    for url in service_roles.values():
        owned = empty_database.execute(
            "SELECT count(*) FROM pg_class WHERE relowner = "
            "(SELECT oid FROM pg_roles WHERE rolname = %s LIMIT 1) LIMIT 1", [role_name(url)]
        ).fetchone()
        assert owned == (0,)


def test_create_schema_is_safe_for_a_role_without_create_privilege(empty_database, data_dir,
                                                                    service_roles):
    bootstrap.main([])

    with psycopg.connect(service_roles["worker"]) as worker:
        create_schema(worker)                              # every table exists: nothing to do
        # Why create_schema checks first: IF NOT EXISTS alone still needs CREATE on the schema.
        with pytest.raises(errors.InsufficientPrivilege):
            worker.execute(CREATE_TABLE_STATEMENT)


# --------------------------------------------------------------------------- #
#                          Failures and exit codes                            #
# --------------------------------------------------------------------------- #

def test_without_the_passwords_it_stops_before_connecting(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://gradcafe@127.0.0.1:1/never_used")  # not even tried

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("worker.bootstrap", run_name="__main__")   # same as: python -m worker.bootstrap

    assert stopped.value.code == 1
    assert "WEB_DB_PASSWORD" in capsys.readouterr().err


def test_an_unreadable_data_file_exits_1(data_dir, service_roles, capsys):
    assert bootstrap.main(["--file", "missing.json"]) == 1
    assert "cannot read the applicant data" in capsys.readouterr().err


def test_one_name_for_both_roles_exits_1_and_changes_nothing(empty_database, data_dir, service_roles,
                                                             monkeypatch, capsys):
    monkeypatch.setenv("WORKER_DB_USER", role_name(service_roles["web"]))

    assert bootstrap.main([]) == 1

    assert "different names" in capsys.readouterr().err
    assert not load_data.table_exists(empty_database, "applicants")      # rolled back


@pytest.mark.parametrize(
    ("url", "message"),
    [("dbname='unterminated", "connection settings are not valid"),
     ("postgresql://gradcafe@127.0.0.1:1/gradcafe_test", "cannot connect to PostgreSQL")],
    ids=["unparseable", "unreachable"],
)
def test_bad_connection_settings_exit_2(data_dir, service_roles, monkeypatch, capsys, url, message):
    monkeypatch.setenv("DATABASE_URL", url)

    assert bootstrap.main([]) == 2
    assert message in capsys.readouterr().err


def test_a_failed_load_exits_3_and_leaves_nothing_behind(empty_database, data_dir, service_roles, capsys):
    load_data_file(data_dir, [{"p_id": 1, "program": "Physics"},
                              {"p_id": 2**40, "program": "too big for INTEGER"}])

    assert bootstrap.main([]) == 3

    assert "query failed" in capsys.readouterr().err
    for table in TABLES:                                  # one transaction: nothing was created
        assert not load_data.table_exists(empty_database, table)
    assert role_privileges(empty_database, role_name(service_roles["web"])) == {"table_privileges": {}}
