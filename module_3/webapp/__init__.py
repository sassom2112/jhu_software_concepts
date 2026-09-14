"""
webapp - Flask front end for the Module 3 Grad Café analysis.

JHU EN.605.256 Modern Software Concepts in Python - Module 3.

create_app() builds the application; run.py starts it.  Database reads go
through the SQLAlchemy Applicant model (orm_queries.py); "Pull Data" runs
pull_data.py as a separate process so a long scrape never blocks the page.
"""

from __future__ import annotations

import os
import secrets

from flask import Flask


def create_app() -> Flask:
    """Application factory."""
    app = Flask(__name__)
    # Flash messages need a secret key.  Use FLASK_SECRET_KEY when set; otherwise
    # a random key per process (nothing secret is stored in the repository).
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

    from .routes import bp

    app.register_blueprint(bp)
    return app
