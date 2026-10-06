"""
conftest.py - fixtures shared by every test file in this folder.

pytest find this file automatically (w/ no import needed anywhere else) and 
makes every function decorated with @pytest.fixture available to any test 
that names it as a parameter.
"""

from __future__ import annotations


import sys 
import time
import urllib.request
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

# Put module_6/src on sys.path, so the tests also run without `pip install -e .`.
# The path is relative to THIS file, so it works from any directory.
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from web.app import create_app      # noqa: E402 (must come after the sys.path fix above)
from db.db_config import get_database_url   # noqa: E402
from db.load_data import connect, create_table   # noqa: E402
from worker.etl.scrape import GradCafeScraper   # noqa: E402
from fake_gradcafe import FakeSite   # noqa: E402  (tests/fake_gradcafe.py)

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


# --------------------------------------------------------------------------- #
#        Environment: a developer's own database settings never leak in       #
# --------------------------------------------------------------------------- #

# The web app's DB_* settings and db_roles.py's APP_DB_* settings (see
# .env.example).  Anyone who has sourced their .env has them set, and they
# could change who the tests log in as.  The tests connect through
# DATABASE_URL only, so these are removed before anything else runs.
DEVELOPER_SETTINGS = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
                      "APP_DB_USER", "APP_DB_PASSWORD")


@pytest.fixture(scope="session", autouse=True)
def ignore_developer_db_settings():
    """Remove DEVELOPER_SETTINGS from the environment while the tests run.

    autouse=True: every test gets this without asking for it by name.
    scope="session": it runs once, before every other fixture -- including the
    session-wide database_url guard below, so the guard checks exactly the
    settings the tests then connect with.  pytest.MonkeyPatch.context() is the
    session-wide version of the monkeypatch fixture: it puts everything back
    when the session ends.  A test may still set any of these itself with
    monkeypatch.setenv(...).
    """
    with pytest.MonkeyPatch.context() as monkeypatch:
        for name in DEVELOPER_SETTINGS:
            monkeypatch.delenv(name, raising=False)
        yield


# --------------------------------------------------------------------------- #
#             Database fixtures: a real (throwaway) test database             #
# --------------------------------------------------------------------------- #

def make_raw_entry(result_id: int, **overrides) -> dict:
    """One entry shaped exactly like GradCafeScraper produces it (before clean.py)."""
    entry = {
        "result_id": result_id,
        "url": f"https://www.thegradcafe.com/result/{result_id}",
        "school_text": "Johns Hopkins University",
        "program_text": "Computer Science",
        "degree_text": "Masters",
        "date_added_text": "Sep 20, 2026",
        "decision_text": "Accepted on Sep 18",
        "tags_text": ["Fall 2027", "International", "GPA 3.90"],
        "comment_text": "Test entry - not real data.",
        "listing_json": None,
        "source_page_url": "https://www.thegradcafe.com/survey?page=1",
        "scraped_at": "2026-09-20T12:00:00+00:00",
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def raw_entries() -> list[dict]:
    """Three fresh fake scraper entries (new dicts every test, so edits never leak)."""
    return [make_raw_entry(9_000_001), make_raw_entry(9_000_002), make_raw_entry(9_000_003)]

@pytest.fixture
def entry_factory():
    """make_raw_entry itself, so a test can build entries with exactly the values it needs."""
    return make_raw_entry

@pytest.fixture(scope="session")
def database_url() -> str:
    """The database the db tests will use -- refused unless its name ends in _test.

    TRUNCATE wipes a table, so this guard is what keeps a mis-set DATABASE_URL
    from ever emptying the real gradcafe database.
    """
    url = get_database_url()
    dbname = conninfo_to_dict(url).get("dbname") or ""
    if not dbname.endswith("_test"):
        pytest.fail(
            f"refusing to run database tests against {dbname or 'the default database'!r}: "
            "set DATABASE_URL to a database whose name ends in _test"
        )
    return url


@pytest.fixture
def db_conn(database_url):
    """An autocommit connection to an EMPTY applicants table in the test database."""
    conn = connect()
    conn.autocommit = True
    create_table(conn)
    conn.execute("TRUNCATE applicants")
    yield conn
    conn.close()


@pytest.fixture
def db_app(db_conn, fake_scraper):
    """The REAL loader and REAL queries against the test database; only the scraper is fake."""
    return create_app(scrape_fn=fake_scraper)


@pytest.fixture
def db_client(db_app):
    return db_app.test_client()

# --------------------------------------------------------------------------- #
#              Scraper fixtures: no test ever reaches the internet            #
# --------------------------------------------------------------------------- #

@pytest.fixture
def scraper(tmp_path):
    """A scraper whose data folder (robots copy, progress log, cached pages) is a temporary folder."""
    return GradCafeScraper(data_dir=tmp_path)

@pytest.fixture
def fake_site(monkeypatch):
    """Replace the internet for one test.

    urllib.request.urlopen is answered by a FakeSite, and time.sleep() is
    recorded in fake_site.sleeps instead of actually waiting.
    """
    site = FakeSite()
    monkeypatch.setattr(urllib.request, "urlopen", site.urlopen)
    monkeypatch.setattr(time, "sleep", site.sleeps.append)
    return site

