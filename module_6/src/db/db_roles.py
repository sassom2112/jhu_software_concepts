"""
db_roles.py - Create the least-privilege database roles the web and worker services log in as.

JHU EN.605.256 Modern Software Concepts in Python - Module 6.

The table owner (the administrator who runs ``python -m worker.bootstrap`` or
load_data.py) may do anything to the tables, including drop them.  The two
services need far less, so each logs in as its own role that can do exactly
its job (the RoleSpec constants below):

  * WEB_ROLE (default name gradcafe_web): the web app only reads.  SELECT on
    applicants (the search API) and on analysis_snapshot (the analysis page);
    nothing on ingestion_watermarks, and no INSERT, UPDATE or DELETE anywhere.
  * WORKER_ROLE (default name gradcafe_worker): the worker adds new entries and
    keeps its bookkeeping.  SELECT and INSERT on applicants; SELECT, INSERT and
    UPDATE on ingestion_watermarks and analysis_snapshot (both are UPSERTed).
    It can never UPDATE or DELETE an entry.

Both roles get the same attributes:

  * LOGIN, and nothing else: NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
    NOREPLICATION, NOBYPASSRLS, and NOINHERIT, so a role granted to it later
    does not silently widen what it can do;
  * CONNECT on this database and USAGE on the public schema, enough to reach
    the tables; no CREATE in the schema, no TRUNCATE, and they never own a
    table, so they cannot ALTER or DROP one either;
  * limits on how much they can use: at most ROLE_CONNECTION_LIMIT connections,
    and every statement is cancelled after ROLE_STATEMENT_TIMEOUT, so even a
    compromised service cannot tie the server up with endless queries.

load_data.load_records() stages each batch in a temporary table.  That needs no
grant here: PostgreSQL gives every role (PUBLIC) the TEMPORARY privilege on a
database by default, and a temporary table belongs to the session that made it
and disappears with it.  Since PostgreSQL 15, PUBLIC may no longer CREATE in the
public schema; on an older server, revoke that by hand (REVOKE CREATE ON SCHEMA
public FROM PUBLIC) or the roles could still create tables there.

Provisioning is code rather than a paragraph in a README:
build_role_statements() composes the SQL and touches nothing,
provision_role() runs it in one transaction, and role_privileges() reads back
what a role may do.  Running it again resets the role: its attributes, its
password and its privileges on every table of the schema.

Usage (connected as the table owner, a superuser such as the gradcafe role of
the Docker container and of CI; PostgreSQL 16+ lets only a superuser restate
NOSUPERUSER, NOREPLICATION and NOBYPASSRLS when the role already exists)::

    source .env    # WEB_DB_USER/WEB_DB_PASSWORD and WORKER_DB_USER/WORKER_DB_PASSWORD
    DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe python -m db.db_roles

``python -m worker.bootstrap`` does the same as part of initializing the
database.  The passwords are never printed.  CREATE ROLE cannot take a bound
parameter, so a password reaches the server as a quoted literal and PostgreSQL
stores only its SCRAM hash; do not run this while the server logs every
statement (log_statement set to ddl or all).  Each service then logs in with
DB_USER and DB_PASSWORD (or a DATABASE_URL) naming its role (see .env.example).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from db.db_config import (SNAPSHOT_TABLE, TABLE_NAME, WATERMARK_TABLE, describe_target,
                          run_with_connection)
from db.load_data import create_schema
from db.query_limits import MAX_LIMIT

SCHEMA_NAME = "public"
MANAGED_TABLES = (TABLE_NAME, WATERMARK_TABLE, SNAPSHOT_TABLE)
ROLE_CONNECTION_LIMIT = 10      # each service needs a few; nobody needs hundreds
ROLE_STATEMENT_TIMEOUT = "30s"  # the services' queries take milliseconds; anything longer is abuse

# The only table privileges a service role may ever be given.  Each is a fixed SQL
# keyword written here in the code, never taken from any input.
ALLOWED_PRIVILEGES = ("SELECT", "INSERT", "UPDATE")

# A plain, unquoted PostgreSQL name: lower case, at most 63 characters.  Used with
# fullmatch, so not even a trailing newline gets through.
ROLE_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# The pg_roles columns role_privileges() reports: of the true/false flags only
# rolcanlogin should be true; rolconnlimit is the connection limit and rolconfig
# lists the role's own settings (its statement_timeout).
ROLE_ATTRIBUTES = ("rolsuper", "rolcreatedb", "rolcreaterole", "rolinherit",
                   "rolreplication", "rolbypassrls", "rolcanlogin", "rolconnlimit", "rolconfig")


@dataclass(frozen=True)
class RoleSpec:
    """One service's database role: its default name, the environment variables that
    name it and hold its password, and exactly which privileges it gets on which table."""

    service: str
    default_name: str
    user_variable: str
    password_variable: str
    grants: tuple[tuple[str, tuple[str, ...]], ...]


WEB_ROLE = RoleSpec(
    service="web",
    default_name="gradcafe_web",
    user_variable="WEB_DB_USER",
    password_variable="WEB_DB_PASSWORD",
    grants=((TABLE_NAME, ("SELECT",)),
            (SNAPSHOT_TABLE, ("SELECT",))),
)
WORKER_ROLE = RoleSpec(
    service="worker",
    default_name="gradcafe_worker",
    user_variable="WORKER_DB_USER",
    password_variable="WORKER_DB_PASSWORD",
    grants=((TABLE_NAME, ("SELECT", "INSERT")),
            (WATERMARK_TABLE, ("SELECT", "INSERT", "UPDATE")),
            (SNAPSHOT_TABLE, ("SELECT", "INSERT", "UPDATE"))),
)
SERVICE_ROLES = (WEB_ROLE, WORKER_ROLE)

# Built with psycopg's sql module, as everywhere else: role, database, schema and
# table names become sql.Identifier and values are bound placeholders.  The one
# exception is the password: CREATE/ALTER ROLE cannot take a bound parameter,
# so it becomes an sql.Literal, which psycopg quotes and escapes.
ROLE_STATEMENT = sql.SQL(
    "{verb} ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
    "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {connections} PASSWORD {password}"
)
TIMEOUT_STATEMENT = sql.SQL("ALTER ROLE {role} SET statement_timeout = {timeout}")
# Start from nothing on every table of the schema...
REVOKE_STATEMENT = sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {tables} FROM {role}")
ACCESS_STATEMENTS = (
    sql.SQL("GRANT CONNECT ON DATABASE {database} TO {role}"),
    sql.SQL("GRANT USAGE ON SCHEMA {schema} TO {role}"),
)
# ...then grant exactly what the RoleSpec lists, table by table.
GRANT_STATEMENT = sql.SQL("GRANT {privileges} ON TABLE {table} TO {role}")

# Where provisioning runs: the database to grant CONNECT on and the role this
# connection logs in as; then the owners of the tables.
CONTEXT_STATEMENT = sql.SQL("SELECT current_database(), current_user LIMIT 1")
OWNERS_STATEMENT = sql.SQL(
    "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = {schema} "
    "AND tablename = ANY({tables}) LIMIT {limit}"
).format(schema=sql.Placeholder("schema"), tables=sql.Placeholder("tables"),
         limit=sql.Placeholder("limit"))
ROLE_EXISTS_STATEMENT = sql.SQL("SELECT 1 FROM pg_roles WHERE rolname = {role} LIMIT 1").format(
    role=sql.Placeholder("role")
)
ROLE_ATTRIBUTES_STATEMENT = sql.SQL(
    "SELECT {columns} FROM pg_roles WHERE rolname = {role} LIMIT 1"
).format(
    columns=sql.SQL(", ").join(sql.Identifier(name) for name in ROLE_ATTRIBUTES),
    role=sql.Placeholder("role"),
)
TABLE_PRIVILEGES_STATEMENT = sql.SQL(
    "SELECT DISTINCT table_name, privilege_type FROM information_schema.role_table_grants "
    "WHERE grantee = {role} AND table_schema = {schema} "
    "ORDER BY table_name, privilege_type LIMIT {limit}"
).format(
    role=sql.Placeholder("role"),
    schema=sql.Placeholder("schema"),
    limit=sql.Placeholder("limit"),
)


class RoleError(ValueError):
    """A role name or password was refused; the message never contains the password."""


def validate_role(spec: RoleSpec, role: str, password: str) -> None:
    """Raise RoleError unless *role* is a plain lower-case name and *password* is not blank."""
    if not ROLE_NAME_PATTERN.fullmatch(role):
        raise RoleError(f"the {spec.service} role name ({spec.user_variable}) must be 1-63 "
                        "lower-case letters, digits or _, not starting with a digit")
    if not password.strip():
        raise RoleError(f"the {spec.service} role needs a password: set "
                        f"{spec.password_variable} (it is never printed)")


def _privilege_list(privileges: tuple[str, ...]) -> sql.Composed:
    """``SELECT, INSERT`` from a RoleSpec; anything outside ALLOWED_PRIVILEGES is refused."""
    refused = [name for name in privileges if name not in ALLOWED_PRIVILEGES]
    if refused or not privileges:
        raise RoleError(f"refusing to grant {refused or 'nothing'}: only "
                        f"{', '.join(ALLOWED_PRIVILEGES)} may be granted")
    return sql.SQL(", ").join(sql.SQL(name) for name in privileges)


def build_role_statements(spec: RoleSpec, role: str, password: str, database: str,
                          exists: bool) -> list[sql.Composed]:
    """The statements that make *role* the least-privilege role described by *spec*.  No I/O.

    CREATE ROLE for a new role, or ALTER ROLE with the same options and the new
    password when *exists*, so running again resets it; then its privileges on
    every table of the schema are revoked and exactly spec.grants are granted
    back.  The first statement carries the password: never print or log these
    statements.  Raises RoleError for an unusable role name, a blank password
    or a privilege outside ALLOWED_PRIVILEGES.
    """
    validate_role(spec, role, password)
    names = {
        "role": sql.Identifier(role),
        "database": sql.Identifier(database),
        "schema": sql.Identifier(SCHEMA_NAME),
    }
    statements = [
        ROLE_STATEMENT.format(
            verb=sql.SQL("ALTER") if exists else sql.SQL("CREATE"),
            role=names["role"],
            connections=sql.Literal(ROLE_CONNECTION_LIMIT),
            password=sql.Literal(password),
        ),
        TIMEOUT_STATEMENT.format(role=names["role"], timeout=sql.Literal(ROLE_STATEMENT_TIMEOUT)),
        REVOKE_STATEMENT.format(
            tables=sql.SQL(", ").join(sql.Identifier(table) for table in MANAGED_TABLES),
            role=names["role"],
        ),
    ]
    statements += [template.format(**names) for template in ACCESS_STATEMENTS]
    statements += [
        GRANT_STATEMENT.format(privileges=_privilege_list(privileges),
                               table=sql.Identifier(table), role=names["role"])
        for table, privileges in spec.grants
    ]
    return statements


def provision_role(conn: psycopg.Connection, spec: RoleSpec, role: str, password: str) -> None:
    """Create *role*, or reset it if it already exists, as the least-privilege *spec* role.

    The tables must exist (load_data.create_schema).  Everything runs in one
    transaction: it all happens or nothing does.  RoleError is raised for an
    unusable name or a blank password, and for the role this connection logs
    in as or the owner of a table, because resetting either would strip the
    administrator's own rights.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(CONTEXT_STATEMENT)
        database, admin = cur.fetchone()
        cur.execute(OWNERS_STATEMENT, {"schema": SCHEMA_NAME, "tables": list(MANAGED_TABLES),
                                       "limit": len(MANAGED_TABLES)})
        owners = {row[0] for row in cur.fetchall()}
        if role == admin or role in owners:
            raise RoleError(f"refusing to change role {role}: it is the role this connection "
                            "logs in as or the owner of a table; choose another "
                            f"{spec.user_variable}")
        cur.execute(ROLE_EXISTS_STATEMENT, {"role": role})
        exists = cur.fetchone() is not None
        for statement in build_role_statements(spec, role, password, database, exists):
            cur.execute(statement)


def role_settings(spec: RoleSpec) -> tuple[str, str]:
    """(role name, password) for *spec* from the environment; the name defaults to
    spec.default_name.  Raises RoleError, before anything connects, if either is unusable."""
    role = os.environ.get(spec.user_variable, "").strip() or spec.default_name
    password = os.environ.get(spec.password_variable, "")
    validate_role(spec, role, password)
    return role, password


def provision_roles(conn: psycopg.Connection,
                    roles: list[tuple[RoleSpec, str, str]]) -> None:
    """Provision every (spec, role name, password) in *roles*.  The names must all differ,
    so the web app can never end up with the worker's privileges or the other way round."""
    names = [role for _spec, role, _password in roles]
    if len(set(names)) != len(names):
        raise RoleError("the web and worker roles need different names "
                        f"({WEB_ROLE.user_variable} and {WORKER_ROLE.user_variable})")
    for spec, role, password in roles:
        provision_role(conn, spec, role, password)


def role_privileges(conn: psycopg.Connection, role: str) -> dict[str, object]:
    """What *role* may do: each ROLE_ATTRIBUTES flag from pg_roles, plus "table_privileges",
    {table: privileges in sorted order} for every table of the schema it has any on."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(ROLE_ATTRIBUTES_STATEMENT, {"role": role})
        report = dict(cur.fetchone() or {})  # no attributes at all if the role does not exist
        cur.execute(TABLE_PRIVILEGES_STATEMENT, {"role": role, "schema": SCHEMA_NAME,
                                                 "limit": MAX_LIMIT})
        tables: dict[str, list[str]] = {}
        for row in cur.fetchall():
            tables.setdefault(row["table_name"], []).append(row["privilege_type"])
    report["table_privileges"] = tables
    return report


# --------------------------------------------------------------------------- #
#                                Command line                                 #
# --------------------------------------------------------------------------- #


def _provision_and_report(conn: psycopg.Connection,
                          roles: list[tuple[RoleSpec, str, str]]) -> dict[str, dict]:
    """Make sure the tables exist, provision every role, then read back what each may do."""
    create_schema(conn)
    provision_roles(conn, roles)
    return {role: role_privileges(conn, role) for _spec, role, _password in roles}


def print_report(reports: dict[str, dict]) -> None:
    """Show what each role may do now; *reports* never hold a password."""
    for role, report in reports.items():
        print(f"Role {role} is ready (provisioned through {describe_target()}; "
              "its password is set but never shown)")
        for name in ROLE_ATTRIBUTES:
            print(f"  {name:<15} {report[name]}")
        for table in MANAGED_TABLES:
            privileges = report["table_privileges"].get(table, [])
            print(f"  privileges on {table}: {', '.join(privileges) or 'none'}")


def main(argv: list[str] | None = None) -> int:
    """Command line: create or reset the web and worker roles from the environment.

    Returns 0, 1 (a password is missing, or a role name was refused), 2 (cannot
    connect) or 3 (a statement failed, so nothing was changed).
    """
    parser = argparse.ArgumentParser(
        description="Create or reset the least-privilege PostgreSQL roles of the web and "
                    "worker services.",
        epilog="WEB_DB_USER and WORKER_DB_USER name the roles (defaults gradcafe_web and "
               "gradcafe_worker); WEB_DB_PASSWORD and WORKER_DB_PASSWORD are required and "
               "never printed.  Connect as the table owner through DATABASE_URL.",
    )
    parser.parse_args(argv)
    try:
        # Check every setting before connecting: nothing to do without the passwords.
        roles = [(spec, *role_settings(spec)) for spec in SERVICE_ROLES]
        status, reports = run_with_connection(lambda conn: _provision_and_report(conn, roles))
    except RoleError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if reports is not None:
        print_report(reports)
    return status


if __name__ == "__main__":
    sys.exit(main())
