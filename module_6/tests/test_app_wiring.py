"""
test_app_wiring.py - The paths the button tests never take: real defaults and failures.

Every other test hands create_app() a fake for each piece.  These tests check
what happens with the real defaults in place (the redirect, the start-up
script, the production scraper wiring, the ORM model) and when the database
is down.  Nothing here touches the internet: monkeypatch swaps the one
network-facing class, GradCafeScraper, for a stand-in.
"""

from __future__ import annotations

import runpy

import flask
import pytest
from bs4 import BeautifulSoup

from web.app import create_app, services
from worker.etl import scrape
from worker.etl.models import Applicant


@pytest.mark.web
def test_root_redirects_to_analysis(client):
    response = client.get("/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/analysis")


@pytest.mark.web
def test_analysis_page_shows_a_banner_when_the_database_is_down():
    def unreachable_database():
        raise ConnectionError("no route to host")

    client = create_app(query_fn=unreachable_database).test_client()

    response = client.get("/analysis")

    assert response.status_code == 200                  # a friendly page, not a crash
    banner = BeautifulSoup(response.data, "html.parser").find(attrs={"role": "alert"})
    assert "database is not reachable" in banner.get_text()
    assert "ConnectionError" in banner.get_text()


@pytest.mark.web
def test_run_py_starts_the_server_on_the_default_address(monkeypatch):
    started = []
    monkeypatch.setattr(flask.Flask, "run", lambda app, **options: started.append(options))
    for name in ("FLASK_HOST", "PORT", "FLASK_DEBUG"):
        monkeypatch.delenv(name, raising=False)

    runpy.run_module("web.run", run_name="__main__")        # same as: python src/web/run.py

    assert started == [{"host": "127.0.0.1", "port": 8080, "debug": False}]


@pytest.mark.db
def test_default_scrape_fn_skips_ids_already_stored(db_client, fake_scraper, raw_entries, monkeypatch):
    fake_scraper.rows = raw_entries
    db_client.post("/pull-data")                        # three rows are now stored

    seen = {}

    class RecordingScraper:
        """Stands in for GradCafeScraper: records its arguments instead of going online."""

        def __init__(self, cache_html):
            seen["cache_html"] = cache_html

        def scrape_new_entries(self, known_ids, max_pages):
            seen["known_ids"], seen["max_pages"] = known_ids, max_pages
            return ["a new entry"], 1, None

    monkeypatch.setattr(scrape, "GradCafeScraper", RecordingScraper)

    assert services.default_scrape_fn() == ["a new entry"]
    assert seen == {"cache_html": False, "known_ids": {9_000_001, 9_000_002, 9_000_003}, "max_pages": 50}


@pytest.mark.db
def test_applicant_repr_names_the_key_fields():
    applicant = Applicant(p_id=7, program="Physics, MIT", status="Accepted", term="Fall 2026")

    assert repr(applicant) == "<Applicant p_id=7 program='Physics, MIT' status='Accepted' term='Fall 2026'>"


@pytest.mark.db
def test_models_refuses_unparseable_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "host=localhost port=abc")

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("worker.etl.models", alter_sys=True)      # a fresh import of models.py

    assert "connection settings are not valid" in str(stopped.value)
    assert "abc" not in str(stopped.value)              # never echo the bad setting back