"""
test_integration_end_to_end.py - The whole app, start to finish.

Pull Data -> Update Analysis -> the analysis page, with only the scraper
faked.  The loader writes to the *_test database and the page is computed by
the real SQLAlchemy queries (orm_queries.get_analysis), so these tests show
the pieces work together, not just one at a time.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from load_data import fetch_applicants


def fall_2026_applicant(entry_factory, result_id, citizenship, gpa, decision):
    """A Fall 2026 scraper entry carrying the fields the analysis questions count."""
    return entry_factory(
        result_id,
        tags_text=["Fall 2026", citizenship, f"GPA {gpa}"],
        decision_text=f"{decision} on Sep 18",
    )


def page_results(response) -> dict[tuple[str, str], str]:
    """{(question number, result label): the value shown after "Answer:"} for every result line."""
    soup = BeautifulSoup(response.data, "html.parser")
    results = {}
    for card in soup.select("article.card"):
        number = card.select_one(".card-number").get_text(" ", strip=True).split()[1]
        for line in card.select("div.result"):
            label = line.dt.get_text(strip=True)
            value = line.dd.get_text(" ", strip=True).removeprefix("Answer:").strip()
            results[(number, label)] = value
    return results


def entries_shown(response) -> str:
    """The "entries in the database" number at the top of the page."""
    soup = BeautifulSoup(response.data, "html.parser")
    return soup.select_one(".stat-value").get_text(strip=True)


@pytest.fixture
def first_batch(entry_factory):
    return [
        fall_2026_applicant(entry_factory, 9_100_001, "International", "3.90", "Accepted"),
        fall_2026_applicant(entry_factory, 9_100_002, "American", "3.50", "Rejected"),
        fall_2026_applicant(entry_factory, 9_100_003, "American", "3.70", "Accepted"),
    ]


@pytest.mark.integration
def test_pull_update_render(db_client, fake_scraper, first_batch):
    assert entries_shown(db_client.get("/analysis")) == "0"

    fake_scraper.rows = first_batch
    pull = db_client.post("/pull-data")
    update = db_client.post("/update-analysis")
    page = db_client.get("/analysis")

    assert pull.status_code == 200
    assert pull.get_json()["inserted"] == 3
    assert update.status_code == 200
    assert page.status_code == 200
    assert entries_shown(page) == "3"
    results = page_results(page)
    assert results[("1", "Fall 2026 applicant count")] == "3"
    assert results[("2", "Percent international")] == "33.33%"        # 1 of 3, two decimals
    assert results[("4", "Average GPA of American Fall 2026 applicants")] == "3.60"
    assert results[("6", "Average GPA of accepted Fall 2026 applicants")] == "3.80"


@pytest.mark.integration
def test_overlapping_pulls_keep_rows_unique(db_client, db_conn, fake_scraper, entry_factory, first_batch):
    second_batch = first_batch[1:] + [
        fall_2026_applicant(entry_factory, 9_100_004, "International", "3.80", "Accepted"),
    ]

    fake_scraper.rows = first_batch
    first = db_client.post("/pull-data").get_json()
    fake_scraper.rows = second_batch
    second = db_client.post("/pull-data").get_json()
    page = db_client.get("/analysis")

    assert first["inserted"] == 3
    assert second["inserted"] == 1                  # the two repeats were skipped
    p_ids = [row["p_id"] for row in fetch_applicants(db_conn)]
    assert p_ids == [9_100_004, 9_100_003, 9_100_002, 9_100_001]
    assert entries_shown(page) == "4"
    assert page_results(page)[("2", "Percent international")] == "50.00%"     # 2 of 4


@pytest.mark.integration
def test_running_pull_blocks_both_buttons_until_it_finishes(db_client, fake_scraper, first_batch):
    pull_state = db_client.application.pull_state
    pull_state.try_start()                          # a pull is "in progress"

    page = db_client.get("/analysis")
    button = BeautifulSoup(page.data, "html.parser").find(attrs={"data-testid": "pull-data-btn"})
    assert button.has_attr("disabled")
    assert db_client.post("/update-analysis").status_code == 409
    assert db_client.post("/pull-data").status_code == 409
    assert fake_scraper.calls == 0

    pull_state.finish()                             # ...and now it is done

    fake_scraper.rows = first_batch
    assert db_client.post("/pull-data").status_code == 200
    assert db_client.post("/update-analysis").status_code == 200
    assert entries_shown(db_client.get("/analysis")) == "3"