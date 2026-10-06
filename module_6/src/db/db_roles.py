"""
db_roles.py - Create the least-privilege database role the web app logs in as.

JHU EN.605.256 Modern Software Concepts in Python - Module 5.

The table owner (the administrator who runs load_data.py) may do anything to
the applicants table, including drop it.  The web app needs far less: it reads
rows (the analysis page, the search API, the ids "Pull Data" skips) and adds
new ones ("Pull Data").  So it logs in as its own role that can do exactly that:

  * LOGIN, and nothing else: NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
    NOREPLICATION, NOBYPASSRLS, and NOINHERIT, so a role granted to it later
    does not silently widen what it can do;
  * CONNECT on this database and USAGE on the public schema, enough to reach
    the table;
  * SELECT and INSERT on applicants, and nothing more: no UPDATE, DELETE or
    TRUNCATE, no CREATE in the schema, and it never owns the table, so it
    cannot ALTER or DROP it either;
  * limits on how much it can use: at most APP_CONNECTION_LIMIT connections,
    and every statement is cancelled after APP_STATEMENT_TIMEOUT, so even a
    compromised app cannot tie the server up with endless queries.

load_data.load_records() stages each batch in a temporary table.  That needs no
grant here: PostgreSQL gives every role (PUBLIC) the TEMPORARY privilege on a
database by default, and a temporary table belongs to the session that made it
and disappears with it.  Since PostgreSQL 15, PUBLIC may no longer CREATE in the
public schema; on an older server, revoke that by hand (REVOKE CREATE ON SCHEMA
public FROM PUBLIC) or the role could still create tables there.

Provisioning is code rather than a paragraph in a README:
build_role_statements() composes the SQL and touches nothing,
provision_app_role() runs it in one transaction, and role_privileges() reads
back what the role may do.  Running it again resets the role: its attributes,
its password and its privileges on the table.

Usage (connected as the table owner, a superuser such as the gradcafe role of
the Docker container and of CI; PostgreSQL 16+ lets only a superuser restate
NOSUPERUSER, NOREPLICATION and NOBYPASSRLS when the role already exists)::

    source .env    # sets APP_DB_USER and APP_DB_PASSWORD (see .env.example)
    DATABASE_URL=postgresql://gradcafe@localhost:5432/gradcafe python -m db.db_roles

The password is never printed.  CREATE ROLE cannot take a bound parameter, so
it reaches the server as a quoted literal and PostgreSQL stores only its SCRAM
hash; do not run this while the server logs every statement (log_statement set
to ddl or all).  The web app then logs in with DB_USER and DB_PASSWORD set to
the same two values (see .env.example).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from db.db_config import TABLE_NAME, describe_target, run_with_connection
from db.query_limits import MAX_LIMIT

DEFAULT_APP_ROLE = "gradcafe_app"
SCHEMA_NAME = "public"
APP_CONNECTION_LIMIT = 10      # the web app needs a few; nobody needs hundreds
APP_STATEMENT_TIMEOUT = "30s"  # the app's queries take milliseconds; anything longer is abuse

# A plain, unquoted PostgreSQL name: lower case, at most 63 characters.  Used with
# fullmatch, so not even a trailing newline gets through.
ROLE_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# The pg_roles columns role_privileges() reports: of the true/false flags only
# rolcanlogin should be true; rolconnlimit is the connection limit and rolconfig
# lists the role's own settings (its statement_timeout).
ROLE_ATTRIBUTES = ("rolsuper", "rolcreatedb", "rolcreaterole", "rolinherit",
                   "rolreplication", "rolbypassrls", "rolcanlogin", "rolconnlimit", "rolconfig")

# Built with psycopg's sql module, as everywhere else: role, database, schema and
# table names become sql.Identifier and values are bound placeholders.  The one
# exception is the password: CREATE/ALTER ROLE cannot take a bound parameter,
# so it becomes an sql.Literal, which psycopg quotes and escapes.
ROLE_STATEMENT = sql.SQL(
    "{verb} ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
    "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {connections} PASSWORD {password}"
)
TIMEOUT_STATEMENT = sql.SQL("ALTER ROLE {role} SET statement_timeout = {timeout}")
PRIVILEGE_STATEMENTS = (
    sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {table} FROM {role}"),  # start from nothing...
    sql.SQL("GRANT CONNECT ON DATABASE {database} TO {role}"),
    sql.SQL("GRANT USAGE ON SCHEMA {schema} TO {role}"),
    sql.SQL("GRANT SELECT, INSERT ON TABLE {table} TO {role}"),     # ...then exactly these two
)

# Where provisioning runs: the database to grant CONNECT on, the role this
# connection logs in as, and the owner of the applicants table.
CONTEXT_STATEMENT = sql.SQL(
    "SELECT current_database(), current_user, "
    "(SELECT tableowner FROM pg_tables WHERE schemaname = {schema} AND tablename = {table} "
    "LIMIT 1) LIMIT 1"
).format(schema=sql.Placeholder("schema"), table=sql.Placeholder("table"))
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
    "SELECT DISTINCT privilege_type FROM information_schema.role_table_grants "
    "WHERE grantee = {role} AND table_schema = {schema} AND table_name = {table} "
    "ORDER BY privilege_type LIMIT {limit}"
).format(
    role=sql.Placeholder("role"),
    schema=sql.Placeholder("schema"),
    table=sql.Placeholder("table"),
    limit=sql.Placeholder("limit"),
)


class RoleError(ValueError):
    """A role name or password was refused; the message never contains the password."""


def validate_role(role: str, password: str) -> None:
    """Raise RoleError unless *role* is a plain lower-case name and *password* is not blank."""
    if not ROLE_NAME_PATTERN.fullmatch(role):
        raise RoleError("the role name must be 1-63 lower-case letters, digits or _, "
                        "not starting with a digit")
    if not password.strip():
        raise RoleError("the role needs a password: set APP_DB_PASSWORD (it is never printed)")


def build_role_statements(role: str, password: str, database: str,
                          exists: bool) -> list[sql.Composed]:
    """The statements that make *role* the least-privilege app role.  No I/O.

    CREATE ROLE for a new role, or ALTER ROLE with the same options and the new
    password when *exists*, so running again resets it; then its privileges on
    the table are revoked and granted back as exactly SELECT and INSERT.  The
    first statement carries the password: never print or log these statements.
    Raises RoleError for an unusable role name or a blank password.
    """
    validate_role(role, password)
    names = {
        "role": sql.Identifier(role),
        "table": sql.Identifier(TABLE_NAME),
        "database": sql.Identifier(database),
        "schema": sql.Identifier(SCHEMA_NAME),
    }
    role_statement = ROLE_STATEMENT.format(
        verb=sql.SQL("ALTER") if exists else sql.SQL("CREATE"),
        role=names["role"],
        connections=sql.Literal(APP_CONNECTION_LIMIT),
        password=sql.Literal(password),
    )
    timeout = TIMEOUT_STATEMENT.format(role=names["role"],
                                       timeout=sql.Literal(APP_STATEMENT_TIMEOUT))
    return [role_statement, timeout] + [
        template.format(**names) for template in PRIVILEGE_STATEMENTS
    ]


def provision_app_role(conn: psycopg.Connection, role: str, password: str) -> None:
    """Create *role*, or reset it if it already exists, as the least-privilege app role.

    Everything runs in one transaction: it all happens or nothing does.
    RoleError is raised for an unusable name or a blank password, and for the
    role this connection logs in as or the owner of the applicants table,
    because resetting either would strip the administrator's own rights.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(CONTEXT_STATEMENT, {"schema": SCHEMA_NAME, "table": TABLE_NAME})
        database, admin, owner = cur.fetchone()
        if role in (admin, owner):
            raise RoleError(f"refusing to change role {role}: it is the role this connection "
                            f"logs in as or the owner of {TABLE_NAME}; choose another APP_DB_USER")
        cur.execute(ROLE_EXISTS_STATEMENT, {"role": role})
        exists = cur.fetchone() is not None
        for statement in build_role_statements(role, password, database, exists):
            cur.execute(statement)


def role_privileges(conn: psycopg.Connection, role: str) -> dict[str, object]:
    """What *role* may do: each ROLE_ATTRIBUTES flag from pg_roles, plus
    "table_privileges", its privileges on the applicants table in sorted order."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(ROLE_ATTRIBUTES_STATEMENT, {"role": role})
        report = dict(cur.fetchone() or {})  # no attributes at all if the role does not exist
        cur.execute(TABLE_PRIVILEGES_STATEMENT, {"role": role, "schema": SCHEMA_NAME,
                                                 "table": TABLE_NAME, "limit": MAX_LIMIT})
        report["table_privileges"] = [row["privilege_type"] for row in cur.fetchall()]
    return report


# --------------------------------------------------------------------------- #
#                                Command line                                 #
# --------------------------------------------------------------------------- #


def _provision_and_report(conn: psycopg.Connection, role: str, password: str) -> dict:
    """Provision *role* on *conn*, then read back what it may do."""
    provision_app_role(conn, role, password)
    return role_privileges(conn, role)


def _print_report(role: str, report: dict) -> None:
    """Show what the role may do now; *report* never holds the password."""
    print(f"Role {role} is ready (provisioned through {describe_target()}; "
          "its password is set but never shown)")
    for name in ROLE_ATTRIBUTES:
        print(f"  {name:<15} {report[name]}")
    print(f"  privileges on {TABLE_NAME}: {', '.join(report['table_privileges']) or 'none'}")


def main(argv: list[str] | None = None) -> int:
    """Command line: create or reset the app role from APP_DB_USER and APP_DB_PASSWORD.

    Returns 0, 1 (no password, or a role name that was refused), 2 (cannot
    connect) or 3 (a statement failed, so nothing was changed).
    """
    parser = argparse.ArgumentParser(
        description="Create or reset the least-privilege PostgreSQL role the web app logs in as.",
        epilog="APP_DB_USER names the role (default gradcafe_app); APP_DB_PASSWORD is required "
               "and never printed.  Connect as the table owner through DATABASE_URL.",
    )
    parser.parse_args(argv)
    role = os.environ.get("APP_DB_USER", "").strip() or DEFAULT_APP_ROLE
    password = os.environ.get("APP_DB_PASSWORD", "")

    try:
        validate_role(role, password)  # before connecting: nothing to do without a password
        status, report = run_with_connection(
            lambda conn: _provision_and_report(conn, role, password)
        )
    except RoleError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if report is not None:
        _print_report(role, report)
    return status


if __name__ == "__main__":
    sys.exit(main())
