"""
test_web_service.py - The web service as its own container sees it.

The health check, the status endpoint the page polls, the "Request queued"
banner and its polling script, and the promise that the web image needs
nothing from the worker: no worker code, no SQLAlchemy, no scraper libraries.
Everything here runs on fakes; no database and no broker are needed.
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from bs4 import BeautifulSoup

from web.app import create_app

pytestmark = pytest.mark.web

SRC = Path(__file__).resolve().parent.parent / "src"
COMPUTED_AT = datetime(2026, 10, 5, 18, 0, 0, tzinfo=timezone.utc)
FAKE_ANALYSIS = {"summary": {"total_entries": 30500, "newest_entry": None}, "answers": []}


def must_not_be_called(*_args):
    raise AssertionError("this endpoint must not touch the database or the broker")


# --------------------------------------------------------------------------- #
#                         GET /healthz: no database needed                    #
# --------------------------------------------------------------------------- #

def test_healthz_answers_ok_without_touching_the_database():
    client = create_app(publish_fn=must_not_be_called, query_fn=must_not_be_called,
                        status_fn=must_not_be_called, search_fn=must_not_be_called).test_client()

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "ok"
    assert response.mimetype == "text/plain"


# --------------------------------------------------------------------------- #
#                 GET /api/analysis-status: what the page polls               #
# --------------------------------------------------------------------------- #

def test_analysis_status_reports_when_the_snapshot_was_computed():
    client = create_app(status_fn=lambda: {"computed_at": COMPUTED_AT, "total_entries": 30500}).test_client()

    response = client.get("/api/analysis-status")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "computed_at": "2026-10-05T18:00:00+00:00",
                                   "total_entries": 30500}
    assert response.headers["Cache-Control"] == "no-store"      # every poll asks again


def test_analysis_status_before_any_snapshot():
    client = create_app(status_fn=lambda: None).test_client()

    assert client.get("/api/analysis-status").get_json() == {
        "ok": True, "computed_at": None, "total_entries": None,
    }


def test_analysis_status_when_the_database_is_down(caplog):
    def unreachable():
        raise psycopg.OperationalError('password authentication failed for user "gradcafe_web"')

    client = create_app(status_fn=unreachable).test_client()
    with caplog.at_level(logging.ERROR):
        response = client.get("/api/analysis-status")

    assert response.status_code == 503
    assert response.get_json() == {"ok": False, "error": "the analysis status could not be read"}
    assert "gradcafe_web" not in response.get_data(as_text=True) + caplog.text
    assert "OperationalError" in caplog.text


# --------------------------------------------------------------------------- #
#               The page: computed-at time, banner and polling script         #
# --------------------------------------------------------------------------- #

def page(query_fn):
    client = create_app(publish_fn=lambda kind: None, query_fn=query_fn).test_client()
    return BeautifulSoup(client.get("/analysis").get_data(as_text=True), "html.parser")


def test_the_page_shows_when_the_analysis_was_computed():
    soup = page(lambda: {**FAKE_ANALYSIS, "computed_at": COMPUTED_AT})

    assert soup.select_one("[data-testid=computed-at]").get_text(strip=True) == \
        "October 05, 2026 at 06:00:00 PM UTC"
    assert soup.select_one("#analysis-page")["data-computed-at"] == "2026-10-05T18:00:00+00:00"


def test_an_analysis_without_a_time_shows_a_dash():
    soup = page(lambda: FAKE_ANALYSIS)

    assert soup.select_one("[data-testid=computed-at]").get_text(strip=True) == "–"


def test_the_page_has_a_queued_banner_and_polls_the_status_endpoint():
    soup = page(lambda: FAKE_ANALYSIS)
    script = soup.find("script").get_text()

    banner = soup.select_one("#action-message")
    assert banner["role"] == "status" and banner.has_attr("hidden")    # shown after a 202
    assert soup.select_one("#analysis-page")["data-status-url"] == "/api/analysis-status"
    assert "Request queued" in script
    assert "status === 202" in script
    assert "/pull-data" in script and "/update-analysis" in script
    assert "window.location.reload()" in script                         # once computed_at changes


# --------------------------------------------------------------------------- #
#                  The web image needs nothing from the worker                #
# --------------------------------------------------------------------------- #

# Packages that exist only in the worker image.
WORKER_ONLY = ("worker", "sqlalchemy", "bs4", "lxml")


def imported_modules(path: Path):
    """Every module name a Python file imports (absolute imports only; this project uses no others
    outside web.app's own `from . import`)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module


def test_no_web_or_db_module_imports_worker_only_packages():
    files = sorted((SRC / "web").rglob("*.py")) + sorted((SRC / "db").rglob("*.py"))
    found = [
        f"{path.relative_to(SRC)} imports {name}"
        for path in files
        for name in imported_modules(path)
        if name.split(".")[0] in WORKER_ONLY
    ]

    assert SRC / "web" / "publisher.py" in files       # the scan really read the web package
    assert found == []


def test_the_web_app_starts_with_the_worker_packages_missing():
    # A fresh interpreter in which importing any worker-only package fails, as in the web image.
    code = (
        "import sys\n"
        f"sys.modules.update(dict.fromkeys({WORKER_ONLY!r}))\n"   # None in sys.modules = ImportError
        "import web.run\n"
        "print(sorted(rule.rule for rule in web.run.app.url_map.iter_rules()))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}

    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                            timeout=60, check=False)

    assert result.returncode == 0, result.stderr
    assert "/healthz" in result.stdout and "/pull-data" in result.stdout
