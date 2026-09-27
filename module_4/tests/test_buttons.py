"""
tests/test_buttons.py

Covers: POST /pull-data, POST /update-analysis, and the busy-gating rule that
keeps two pulls (or a pull and an update) from running at once.
"""

from __future__ import annotations

import pytest

from webapp import create_app  # sys.path already has src/ on it, thanks to conftest.py


# ---------------------------------------------------------------------------
# POST /pull-data
# ---------------------------------------------------------------------------

@pytest.mark.buttons
def test_pull_data_returns_200_and_triggers_the_loader_with_the_scrapers_rows(client, fake_scraper, fake_loader):
    fake_scraper.rows = [
        {"result_id": 1, "url": "https://www.thegradcafe.com/result/1", "school_text": "Test University"},
        {"result_id": 2, "url": "https://www.thegradcafe.com/result/2", "school_text": "Test University"},
    ]

    response = client.post("/pull-data")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert fake_scraper.calls == 1
    # the loader must receive exactly the rows the scraper returned
    assert fake_loader.calls == [fake_scraper.rows]


@pytest.mark.buttons
def test_pull_data_returns_409_when_a_pull_is_already_running(app, client, fake_scraper, fake_loader):
    app.pull_state.try_start()  # force the busy state directly; no real second request needed

    response = client.post("/pull-data")

    assert response.status_code == 409
    assert response.get_json() == {"busy": True}
    # gated before it ever touched the scraper or loader
    assert fake_scraper.calls == 0
    assert fake_loader.calls == []


@pytest.mark.buttons
def test_pull_data_returns_500_and_clears_busy_state_when_the_loader_fails(fake_scraper):
    def raising_loader(rows):
        raise RuntimeError("insert failed")

    app = create_app(scrape_fn=fake_scraper, load_fn=raising_loader, query_fn=lambda: {})
    client = app.test_client()

    response = client.post("/pull-data")

    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert "insert failed" in body["error"]
    # the route's finally: block must always run, so a failed pull never leaves the app stuck "busy"
    assert app.pull_state.is_running is False


# ---------------------------------------------------------------------------
# POST /update-analysis
# ---------------------------------------------------------------------------

@pytest.mark.buttons
def test_update_analysis_returns_200_when_not_busy(client):
    response = client.post("/update-analysis")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


@pytest.mark.buttons
def test_update_analysis_returns_409_when_a_pull_is_running(app, client):
    app.pull_state.try_start()

    response = client.post("/update-analysis")

    assert response.status_code == 409
    assert response.get_json() == {"busy": True}