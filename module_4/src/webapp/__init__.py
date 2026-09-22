"""
webapp - Flask front end for the Grad Café analysis (Module 4).

create_app() is the application factory.  Every external dependency the
routes need — the scraper, the loader, the analysis query — is injected as a
keyword argument with a production default, so tests can call

    create_app(scrape_fn=fake_scrape, load_fn=fake_load, query_fn=fake_query)

and get a fully working Flask app wired to fakes, with no real network call
and no real database required unless a test wants one.
"""

from __future__ import annotations

import os
import secrets
from typing import Callable

from flask import Flask

from . import services

ScrapeFn = Callable[[], list[dict]]
LoadFn = Callable[[list[dict]], int]
QueryFn = Callable[[], dict]


def create_app(
    *,
    scrape_fn: ScrapeFn | None = None,
    load_fn: LoadFn | None = None,
    query_fn: QueryFn | None = None,
) -> Flask:
    """Build and configure the Flask application.

    Args:
        scrape_fn: no-argument callable returning newly scraped raw entries.
            Defaults to services.default_scrape_fn (talks to Grad Café).
        load_fn: callable(raw_entries) -> rows inserted.  Defaults to
            services.default_load_fn (cleans, then writes to PostgreSQL).
        query_fn: no-argument callable returning the dict the analysis page
            renders.  Defaults to orm_queries.get_analysis (reads PostgreSQL
            through the SQLAlchemy model).
    """
    app = Flask(__name__)
    # Flash messages need a secret key.  Use FLASK_SECRET_KEY when set; otherwise
    # a random key per process (nothing secret is stored in the repository).
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.config["SCRAPE_FN"] = scrape_fn or services.default_scrape_fn
    app.config["LOAD_FN"] = load_fn or services.default_load_fn
    app.config["QUERY_FN"] = query_fn or _default_query_fn

    # One PullState per application instance (not module-global), so each
    # create_app() call in a test starts idle regardless of other tests.
    app.pull_state = services.PullState()

    from .routes import bp

    app.register_blueprint(bp)
    return app


def _default_query_fn() -> dict:
    """Deferred import: importing webapp must never require a live database."""
    import orm_queries

    return orm_queries.get_analysis()
