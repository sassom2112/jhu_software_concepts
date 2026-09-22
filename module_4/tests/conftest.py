"""
conftest.py - fixtures share by every test file in this folder.

pytest find this file automatically (w/ no import needed anywhere else) and 
makes every function decorated with @pytest.fixture available to any test 
that names it as a parameter.
"""

from __future__ import annotations

import sys 
from pathlib import Path

import pytest

# module_4/src is not an installled package, so tell Pyhton where to find it.
# This is relative to THIS  file, so it works no matter what dir you run pytest from
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from webapp import create_app       # noqa: E402 (must come after the sys.path fix above)

# A realistic-shaped fake for QUERY_FN: same structure orm_queries.get_analysis()
# returns in the real app, so templates render exactly the way they would with
# a real db behind them; only instantly and offline.
FAKE_ANALYSIS = {
    "summary": {"total_entries": 30503, "newest_entry": None},
    "answers": [
        {
            "number": "1",
            "question": "How many entries in the database are from applications who applied for Fall 2026?",
            "lines": [("Fall 2026 applicant count", "30,066")],
            "columns": [],
            "table": [],
        },
        {
            "number": "2",
            "question": "Among entries that provide a nationality classification, what percentage are international students?",
            "lines": [("Percentage International", "46.35%")],
            "columns": [],
            "table": [],
        },
    ],
}

class FakeScraper:
    """Stands in for SCRAPE_FN: returns canned rows and remembers how often it was called"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows if rows is not None else []
        self.calls = 0

    def __call__(self) -> list[dict]:
        self.calls += 1
        return self.rows

    
class FakeLoader:
    """Stands in for LOAD_FN: remembers every batch it was asked to 'insert' instead of touching PostgreSQL."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def __call__(self, rows: list[dict]) -> int:
        self.calls.append(rows)
        return len(rows)


@pytest.fixture
def fake_scraper() -> FakeScraper:
    """A fresh, empty fake scraper for each test (override .rows in the test body if you need data)."""
    return FakeScraper()


@pytest.fixture
def fake_loader() -> FakeLoader:
    """A fresh fake loader for each test."""
    return FakeLoader()


@pytest.fixture
def app(fake_scraper, fake_loader):
    """A Flask app wired entirely to fakes: no network call and no database write is possible here."""
    return create_app(scrape_fn=fake_scraper, load_fn=fake_loader, query_fn=lambda: FAKE_ANALYSIS)


@pytest.fixture
def client(app):
    """Flask's in-process test client, built from the app fixture above."""
    return app.test_client()
        