"""
web.app - Flask front end for the Grad Café analysis (Module 6).

create_app() is the application factory.  Every external dependency the
routes need - the task publisher, the analysis snapshot, its status and the
applicant search - is injected as a keyword argument with a production
default, so tests can call::

    create_app(publish_fn=fake_publish, query_fn=fake_query)

and get a fully working Flask app wired to fakes, with no RabbitMQ and no
database required unless a test wants one.  Importing this package opens no
connection: the defaults connect only when a request needs them.

The web app does no data-modifying work and runs no analysis queries itself:
the buttons publish tasks for the worker (web/publisher.py), and the page reads
the analysis snapshot the worker stored (db/snapshot.py).  It imports nothing
from the worker package and needs neither SQLAlchemy nor the scraper's
libraries, so the web image contains only Flask, psycopg and pika.
"""

from __future__ import annotations

import os
import secrets
from typing import Callable

from flask import Flask

from web import publisher
from web.app.applicant_search import SearchRequest

from . import services
from .routes import bp

PublishFn = Callable[..., None]
QueryFn = Callable[[], dict | None]
StatusFn = Callable[[], dict | None]
SearchFn = Callable[[SearchRequest], list[dict]]


def create_app(
    *,
    publish_fn: PublishFn | None = None,
    query_fn: QueryFn | None = None,
    status_fn: StatusFn | None = None,
    search_fn: SearchFn | None = None,
) -> Flask:
    """Build and configure the Flask application.

    Args:
        publish_fn: callable(kind, payload=None, headers=None) that queues a task
            for the worker and raises if it cannot (see publisher.PUBLISH_ERRORS).
            Defaults to publisher.publish_task (RabbitMQ at RABBITMQ_URL).
        query_fn: no-argument callable returning the dict the analysis page
            renders, or None when no analysis has been computed yet.  Defaults
            to services.default_query_fn (reads the stored analysis snapshot).
        status_fn: no-argument callable returning {"computed_at", "total_entries"}
            of the stored snapshot, or None.  Defaults to
            services.default_status_fn; GET /api/analysis-status serves it.
        search_fn: callable(SearchRequest) -> rows for GET /api/applicants.
            Defaults to services.default_search_fn (a read-only, parameterized
            query; see applicant_search.py).
    """
    app = Flask(__name__)
    # Flask's session signing needs a secret key.  Use FLASK_SECRET_KEY when set;
    # otherwise a random key per process (nothing secret is stored in the repository).
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.config["PUBLISH_FN"] = publish_fn or _default_publish_fn
    app.config["QUERY_FN"] = query_fn or services.default_query_fn
    app.config["STATUS_FN"] = status_fn or services.default_status_fn
    app.config["SEARCH_FN"] = search_fn or services.default_search_fn
    app.register_blueprint(bp)
    return app


def _default_publish_fn(kind: str, payload: dict | None = None,
                        headers: dict | None = None) -> None:
    """Production publisher, looked up at call time (RabbitMQ is needed only now)."""
    publisher.publish_task(kind, payload, headers)
