"""
bootstrap.py - Initialize the database: ``python -m worker.bootstrap``.

The Docker Compose stack runs this once at start-up (the one-shot "init"
service, built from the worker image) before the web app and the worker start.
It connects as the table OWNER through DATABASE_URL and

  1. creates whatever tables are missing (load_data.create_schema): applicants,
     ingestion_watermarks and analysis_snapshot;
  2. creates or resets the two least-privilege service roles (db_roles):
     WEB_DB_USER / WEB_DB_PASSWORD (read-only web app) and WORKER_DB_USER /
     WORKER_DB_PASSWORD (the worker, which may also insert);
  3. loads src/data/applicant_data.json (or --file, inside DATA_DIR) with
     load_data.load_records: INSERT ... ON CONFLICT (p_id) DO NOTHING, so rows
     that are already stored are skipped, never duplicated or changed;
  4. computes the analysis snapshot (analytics.refresh_snapshot) when there is
     none yet, or when step 3 added rows, so the page has results to show
     before the first task arrives.

All four steps run in one transaction: the database is initialized completely
or not at all.  Running it again is harmless: nothing is duplicated, the roles
are reset to the same privileges (and the passwords now in the environment),
and a snapshot that is still current is left alone.  It lives in the worker
package because step 4 needs the worker's analysis code.

Exit codes: 0 done, 1 unusable settings (a missing password, an unreadable data
file), 2 cannot connect, 3 a statement failed (nothing was changed).
The passwords are never printed.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import psycopg

from db.db_config import run_with_connection
from db.db_roles import SERVICE_ROLES, RoleError, RoleSpec, provision_roles, role_settings
from db.load_data import DEFAULT_INPUT, count_rows, create_schema, load_records, read_input
from db.snapshot import snapshot_status
from worker.etl.analytics import refresh_snapshot


@dataclass(frozen=True)
class BootstrapResult:
    """What one run did: the roles it provisioned, the load counts, and whether the
    analysis snapshot was recomputed."""

    roles: tuple[str, ...]
    inserted: int
    already_present: int
    unusable: int
    total_rows: int
    snapshot_refreshed: bool


def bootstrap(conn: psycopg.Connection, records: list[dict],
              roles: list[tuple[RoleSpec, str, str]]) -> BootstrapResult:
    """Schema, roles, data and first snapshot on *conn* (the owner), in one transaction."""
    with conn.transaction():
        create_schema(conn)
        provision_roles(conn, roles)
        inserted, present, unusable = load_records(conn, records)
        refresh = inserted > 0 or snapshot_status(conn) is None
        if refresh:
            refresh_snapshot(conn)
        total = count_rows(conn)
    return BootstrapResult(
        roles=tuple(role for _spec, role, _password in roles),
        inserted=inserted,
        already_present=present,
        unusable=unusable,
        total_rows=total,
        snapshot_refreshed=refresh,
    )


def _print_result(source: str, read: int, result: BootstrapResult) -> None:
    """A short report for the container log; it never contains a password."""
    print("Tables ready: applicants, ingestion_watermarks, analysis_snapshot")
    print(f"Roles ready: {', '.join(result.roles)} (passwords set but never shown)")
    print(f"Read {read:,} records from {source}: inserted {result.inserted:,} new rows; "
          f"{result.already_present:,} were already present; "
          f"{result.unusable:,} unusable records skipped")
    print(f"applicants table now holds {result.total_rows:,} rows")
    if result.snapshot_refreshed:
        print("Analysis snapshot computed")
    else:
        print("Analysis snapshot already up to date; left unchanged")


def main(argv: list[str] | None = None) -> int:
    """Command line: initialize the database; returns 0, 1, 2 or 3 (see the module docstring)."""
    parser = argparse.ArgumentParser(
        description="Create the tables and service roles, load the applicant data and "
                    "compute the first analysis snapshot (safe to run again).",
        epilog="Connect as the table owner through DATABASE_URL.  WEB_DB_PASSWORD and "
               "WORKER_DB_PASSWORD are required and never printed.",
    )
    parser.add_argument("--file", default=DEFAULT_INPUT,
                        help=f"JSON (or .json.gz) file name in the data folder "
                             f"(default {DEFAULT_INPUT}; the folder is DATA_DIR or src/data)")
    args = parser.parse_args(argv)

    try:
        roles = [(spec, *role_settings(spec)) for spec in SERVICE_ROLES]
    except RoleError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    try:
        path, records = read_input(args.file)
    except (OSError, ValueError) as err:
        print(f"error: cannot read the applicant data: {err}", file=sys.stderr)
        return 1
    try:
        status, result = run_with_connection(lambda conn: bootstrap(conn, records, roles))
    except RoleError as err:  # e.g. a role name that is the owner's own
        print(f"error: {err}", file=sys.stderr)
        return 1
    if result is None:
        return status
    _print_result(path.name, len(records), result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
