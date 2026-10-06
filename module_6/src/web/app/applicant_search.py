"""
applicant_search.py - A filtered, sorted, size-limited read of the applicants table.

This is the one place where text typed by a user reaches SQL (through
``GET /api/applicants``), so it follows the SQL-injection rules strictly:

* **Values never become SQL text.**  Every filter value is sent as a bound
  parameter (``%(term)s`` ...), so ``' OR '1'='1`` is just an odd string to
  compare against, never code.
* **Names come from allow-lists.**  The sort column and the filter columns
  must be in :data:`SORTABLE_COLUMNS` / :data:`EXACT_FILTERS` /
  :data:`CONTAINS_FILTERS`; they are then quoted with ``sql.Identifier``.  The
  sort direction is chosen from two fixed SQL fragments, never copied from
  the request.
* **Every query has a LIMIT**, and the number is clamped to 1..MAX_LIMIT.
* **Building and running are separate steps**: :func:`build_search_query`
  returns ``(statement, params)`` and never touches the database;
  :func:`search_applicants` only executes what it was given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from db.db_config import TABLE_NAME
from db.query_limits import LimitError, clamp_limit

RESULT_COLUMNS = ("p_id", "program", "date_added", "status", "term",
                  "us_or_international", "gpa", "gre", "degree", "url")
SORTABLE_COLUMNS = ("p_id", "date_added", "gpa", "gre", "gre_v", "gre_aw",
                    "program", "status", "term")
EXACT_FILTERS = ("term", "status", "degree", "us_or_international")  # case-insensitive equality
CONTAINS_FILTERS = ("program",)                                       # case-insensitive substring
MAX_FILTER_LENGTH = 100
DEFAULT_SORT, DEFAULT_ORDER = "date_added", "desc"

_DIRECTIONS = {"asc": sql.SQL("ASC"), "desc": sql.SQL("DESC")}


class SearchError(ValueError):
    """A request parameter was rejected; the message is safe to show to the caller."""


@dataclass(frozen=True)
class SearchRequest:
    """A validated search: every field has already passed the allow-lists."""

    filters: dict[str, str] = field(default_factory=dict)
    sort: str = DEFAULT_SORT
    order: str = DEFAULT_ORDER
    limit: int = 20


def parse_search_args(args: Mapping[str, str]) -> SearchRequest:
    """Validate raw query-string arguments into a SearchRequest, or raise SearchError.

    Unknown argument names are ignored.  Filter values are trimmed; blank ones
    are dropped; ones longer than MAX_FILTER_LENGTH or containing a NUL
    character are refused.
    """
    filters = {}
    for name in EXACT_FILTERS + CONTAINS_FILTERS:
        value = str(args.get(name, "")).strip()
        if len(value) > MAX_FILTER_LENGTH:
            raise SearchError(f"{name} must be at most {MAX_FILTER_LENGTH} characters")
        if "\x00" in value:  # PostgreSQL text cannot hold NUL; refuse it here, not as a DB error
            raise SearchError(f"{name} contains a character that is not allowed")
        if value:
            filters[name] = value

    sort = str(args.get("sort", DEFAULT_SORT)).strip()
    if sort not in SORTABLE_COLUMNS:
        raise SearchError("sort must be one of: " + ", ".join(SORTABLE_COLUMNS))
    order = str(args.get("order", DEFAULT_ORDER)).strip().lower()
    if order not in _DIRECTIONS:
        raise SearchError("order must be asc or desc")
    try:
        limit = clamp_limit(args.get("limit"))
    except LimitError as err:
        raise SearchError(str(err)) from err
    return SearchRequest(filters=filters, sort=sort, order=order, limit=limit)


def _escape_like(value: str) -> str:
    r"""Make % and _ (and the escape character \ itself) match literally inside LIKE."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_search_query(request: SearchRequest) -> tuple[sql.Composed, dict[str, object]]:
    """Compose the SELECT for a validated request; returns (statement, params). No I/O."""
    conditions = []
    params: dict[str, object] = {}
    for name, value in request.filters.items():
        column = sql.Identifier(name)
        if name in CONTAINS_FILTERS:
            conditions.append(
                sql.SQL("{} ILIKE {} ESCAPE '\\'").format(column, sql.Placeholder(name))
            )
            params[name] = f"%{_escape_like(value)}%"
        else:
            conditions.append(
                sql.SQL("LOWER(TRIM({})) = LOWER({})").format(column, sql.Placeholder(name))
            )
            params[name] = value
    where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("")

    statement = sql.SQL(
        "SELECT {columns} FROM {table}{where} "
        "ORDER BY {sort} {direction} NULLS LAST, {key} DESC LIMIT {limit}"
    ).format(
        columns=sql.SQL(", ").join(sql.Identifier(c) for c in RESULT_COLUMNS),
        table=sql.Identifier(TABLE_NAME),
        where=where,
        sort=sql.Identifier(request.sort),
        direction=_DIRECTIONS[request.order],
        key=sql.Identifier("p_id"),
        limit=sql.Placeholder("limit"),
    )
    params["limit"] = clamp_limit(request.limit)
    return statement, params


def search_applicants(conn: psycopg.Connection, request: SearchRequest) -> list[dict]:
    """Run the search on *conn* and return JSON-ready rows (dates as ISO strings)."""
    statement, params = build_search_query(request)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(statement, params)
        rows = cur.fetchall()
    return [{key: _json_value(value) for key, value in row.items()} for row in rows]


def _json_value(value: object) -> object:
    """Dates become ISO strings ("2026-09-20"); everything else is already JSON-ready."""
    return value.isoformat() if isinstance(value, date) else value
