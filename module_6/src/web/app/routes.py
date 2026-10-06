"""
routes.py - The analysis page, its two buttons, and the small JSON endpoints.

Endpoints::

    GET  /                     redirect to /analysis
    GET  /analysis             the analysis page (the stored snapshot, read through QUERY_FN)
    POST /pull-data            queue a "scrape_new_data" task for the worker -> 202, or 503
    POST /update-analysis      queue a "recompute_analytics" task for the worker -> 202, or 503
    GET  /api/analysis-status  when the snapshot was computed, for the page's poll (STATUS_FN)
    GET  /api/applicants       filtered, sorted, LIMITed rows as JSON (see applicant_search.py)
    GET  /healthz              "ok" without touching the database (container health check)

The buttons never wait for the work itself: they publish a message through
PUBLISH_FN and answer 202 Accepted at once.  RabbitMQ holds the tasks and the
worker runs them one at a time, so clicking twice simply queues two tasks; no
busy flag is needed.  If the task cannot be queued the answer is 503 with a
fixed message; the reason is logged by class name only, because broker errors
can carry host names or credentials.
"""

from __future__ import annotations

from datetime import datetime

import psycopg
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from web.app.applicant_search import SearchError, parse_search_args
from web.publisher import PUBLISH_ERRORS

bp = Blueprint("analysis", __name__)

# The failures a database read can really raise: psycopg's errors, OSError
# (ConnectionError, a refused socket), RuntimeError and ValueError (malformed
# data).  Anything else is a programming error and should surface as one.
EXPECTED_FAILURES = (
    psycopg.Error,
    OSError,
    RuntimeError,
    ValueError,
)

# The two tasks the buttons queue, by the name the worker's task map knows them by.
PULL_TASK = "scrape_new_data"
UPDATE_TASK = "recompute_analytics"
QUEUE_FAILED_MESSAGE = "the task could not be queued; try again in a minute"


def _format_time(moment: datetime | None) -> str | None:
    """*moment* as "October 05, 2026 at 06:00:00 PM UTC" for the page, or None."""
    return moment.strftime("%B %d, %Y at %I:%M:%S %p %Z").strip() if moment else None


@bp.get("/")
def root():
    """Convenience redirect; the analysis page itself lives at /analysis."""
    return redirect(url_for("analysis.index"))


@bp.get("/analysis")
def index():
    """Render the analysis snapshot QUERY_FN returns (None: not computed yet)."""
    analysis, error = None, None
    try:
        analysis = current_app.config["QUERY_FN"]()
    except EXPECTED_FAILURES as err:  # a query failure becomes a friendly banner, not a 500
        error = ("The analysis could not be loaded because the database is not reachable. "
                 f"({err.__class__.__name__})")
    computed_at = analysis.get("computed_at") if analysis else None
    return render_template(
        "analysis.html",
        analysis=analysis,
        error=error,
        computed_at=_format_time(computed_at),
        computed_at_iso=computed_at.isoformat() if computed_at else "",
    )


def _queue(kind: str):
    """Publish one task of *kind*; 202 once RabbitMQ has it, 503 if it could not be queued."""
    try:
        current_app.config["PUBLISH_FN"](kind)
    except PUBLISH_ERRORS as err:  # never echo broker details (hosts, users) to the caller
        current_app.logger.error("could not queue %s: %s", kind, err.__class__.__name__)
        return jsonify(ok=False, queued=False, kind=kind, error=QUEUE_FAILED_MESSAGE), 503
    current_app.logger.info("queued %s", kind)
    return jsonify(ok=True, queued=True, kind=kind), 202


@bp.post("/pull-data")
def pull_data():
    """Ask the worker to fetch Grad Café entries newer than the stored watermark."""
    return _queue(PULL_TASK)


@bp.post("/update-analysis")
def update_analysis():
    """Ask the worker to recompute the analysis snapshot."""
    return _queue(UPDATE_TASK)


@bp.get("/api/analysis-status")
def analysis_status():
    """{"ok", "computed_at" (ISO 8601 or null), "total_entries"} of the stored snapshot.

    The page polls this after a button click and reloads once computed_at
    changes.  One small row, never cached."""
    try:
        status = current_app.config["STATUS_FN"]()
    except EXPECTED_FAILURES as err:
        current_app.logger.error("status read failed: %s", err.__class__.__name__)
        response = jsonify(ok=False, error="the analysis status could not be read")
        response.status_code = 503
    else:
        status = status or {}
        computed_at = status.get("computed_at")
        response = jsonify(ok=True,
                           computed_at=computed_at.isoformat() if computed_at else None,
                           total_entries=status.get("total_entries"))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("/api/applicants")
def api_applicants():
    """Search stored applicants: ?term=&status=&degree=&us_or_international=&program=
    &sort=&order=&limit= .  Invalid parameters get 400; at most MAX_LIMIT rows come back."""
    try:
        search = parse_search_args(request.args)
    except SearchError as err:
        return jsonify(ok=False, error=str(err)), 400
    try:
        rows = current_app.config["SEARCH_FN"](search)
    except EXPECTED_FAILURES as err:  # never echo database details to the caller
        current_app.logger.error("search failed: %s", err.__class__.__name__)
        return jsonify(ok=False, error="the search could not be completed"), 500
    return jsonify(ok=True, limit=search.limit, count=len(rows), rows=rows), 200


@bp.get("/healthz")
def healthz():
    """Liveness for the container health check: answers without touching the database."""
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"}
