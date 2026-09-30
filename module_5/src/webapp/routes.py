"""
routes.py - The analysis page and its two buttons.

Endpoints::

    GET  /                 redirect to /analysis
    GET  /analysis         analysis page (reads the database through QUERY_FN on every request)
    POST /pull-data        run SCRAPE_FN then LOAD_FN; 409 if a pull is already running
    POST /update-analysis  no-op that just confirms the page can be refreshed; 409 if busy
"""

from __future__ import annotations

from datetime import datetime
from http.client import HTTPException

import psycopg
from flask import Blueprint, current_app, jsonify, redirect, render_template, url_for
from sqlalchemy.exc import SQLAlchemyError

bp = Blueprint("analysis", __name__)

# The failures a query or a pull can really raise: database errors from either
# driver layer (psycopg directly, or wrapped by SQLAlchemy), OSError
# (ConnectionError, PermissionError, urllib's URLError), HTTPException (a
# response cut off mid-body), RuntimeError (the scraper's ScrapeBlockedError /
# ScrapeNetworkError / ScrapeStateError) and ValueError (malformed data).
# Anything else is a programming error and should surface as one.
EXPECTED_FAILURES = (
    psycopg.Error,
    SQLAlchemyError,
    OSError,
    HTTPException,
    RuntimeError,
    ValueError,
)


@bp.get("/")
def root():
    """Convenience redirect; the analysis page itself lives at /analysis."""
    return redirect(url_for("analysis.index"))


@bp.get("/analysis")
def index():
    """Render every analysis result the injected QUERY_FN returns."""
    analysis, error = None, None
    try:
        analysis = current_app.config["QUERY_FN"]()
    except EXPECTED_FAILURES as err:  # a query failure becomes a friendly banner, not a 500
        error = ("The analysis could not be loaded because the database is not reachable. "
                 f"({err.__class__.__name__})")
    return render_template(
        "analysis.html",
        analysis=analysis,
        error=error,
        pull_running=current_app.pull_state.is_running,
        computed_at=datetime.now().strftime("%B %d, %Y at %I:%M:%S %p"),
    )


@bp.post("/pull-data")
def pull_data():
    """Fetch new entries and load them; refuses to start a second pull while one runs."""
    if not current_app.pull_state.try_start():
        return jsonify(busy=True), 409
    try:
        raw_entries = current_app.config["SCRAPE_FN"]()
        inserted = current_app.config["LOAD_FN"](raw_entries)
    except EXPECTED_FAILURES as err:  # report the failure as JSON instead of an HTML 500 page
        return jsonify(ok=False, error=str(err)), 500
    finally:
        current_app.pull_state.finish()
    return jsonify(ok=True, inserted=inserted), 200


@bp.post("/update-analysis")
def update_analysis():
    """Confirms the page can be refreshed; performs no database work and never gates on itself."""
    if current_app.pull_state.is_running:
        return jsonify(busy=True), 409
    return jsonify(ok=True), 200
