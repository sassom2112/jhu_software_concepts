"""
routes.py - The analysis page and its two buttons.

GET  /                  analysis page (reads PostgreSQL through the ORM on every request)
POST /pull-data         start a background pull of new Grad Café entries
POST /update-analysis   re-query the database (never starts a scrape)
GET  /pull-status       JSON status used by the page to show pull progress
"""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, url_for
from sqlalchemy.exc import SQLAlchemyError

import orm_queries

from .pull_manager import manager

bp = Blueprint("analysis", __name__)


@bp.get("/")
def index():
    """Render every analysis result from the current database contents."""
    analysis, error = None, None
    try:
        analysis = orm_queries.get_analysis()
    except SQLAlchemyError:
        error = ("The analysis could not be loaded because the database is not reachable. "
                 "Check that PostgreSQL is running and that DATABASE_URL (or PGHOST/PGUSER/PGDATABASE) is set.")
    return render_template(
        "analysis.html",
        analysis=analysis,
        error=error,
        pull=manager.status(),
        computed_at=datetime.now().strftime("%B %d, %Y at %I:%M:%S %p"),
    )


@bp.post("/pull-data")
def pull_data():
    started, message = manager.start()
    flash(message, "info" if started else "warning")
    return redirect(url_for("analysis.index"))


@bp.post("/update-analysis")
def update_analysis():
    if manager.is_running():
        flash("New data is currently being retrieved by Pull Data. The results below were re-read from the "
              "database and include only entries saved so far; click Update Analysis again when the pull "
              "has finished.", "warning")
    else:
        flash("Analysis updated with the most current data in the database.", "success")
    return redirect(url_for("analysis.index"))


@bp.get("/pull-status")
def pull_status():
    return jsonify(manager.status())
