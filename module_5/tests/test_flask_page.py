"""
tests/test_flask_page.py

Covers: the Flask app factory (routes exist) and GET /analysis (status, buttons,
required text).  Every test here is read-only -- none of them POST anything.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# App factory / routing
# ---------------------------------------------------------------------------

@pytest.mark.web
def test_create_app_registers_the_expected_routes(app):
    """create_app() must expose exactly the routes the buttons and page rely on."""
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/analysis" in rules
    assert "/pull-data" in rules
    assert "/update-analysis" in rules


@pytest.mark.web
def test_analysis_route_only_accepts_get(app):
    """/analysis is a read-only page: GET should be allowed, POST should not."""
    rule = next(r for r in app.url_map.iter_rules() if r.rule == "/analysis")
    assert "GET" in rule.methods
    assert "POST" not in rule.methods


# ---------------------------------------------------------------------------
# GET /analysis
# ---------------------------------------------------------------------------

@pytest.mark.web
def test_get_analysis_returns_200(client):
    response = client.get("/analysis")
    assert response.status_code == 200


@pytest.mark.web
def test_analysis_page_shows_both_buttons(client):
    """Both buttons must be present, addressable by their stable data-testid selectors."""
    html = client.get("/analysis").get_data(as_text=True)
    soup = BeautifulSoup(html, "html.parser")

    pull_button = soup.find(attrs={"data-testid": "pull-data-btn"})
    update_button = soup.find(attrs={"data-testid": "update-analysis-btn"})

    assert pull_button is not None, "no element with data-testid='pull-data-btn' found"
    assert update_button is not None, "no element with data-testid='update-analysis-btn' found"
    assert "Pull Data" in pull_button.get_text()
    assert "Update Analysis" in update_button.get_text()


@pytest.mark.web
def test_analysis_page_includes_required_text(client):
    """The page must say 'Analysis' somewhere, and label at least one result with 'Answer:'."""
    html = client.get("/analysis").get_data(as_text=True)
    assert "Analysis" in html
    assert "Answer:" in html