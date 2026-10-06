"""
test_integration_end_to_end.py - The whole app, start to finish.

Click -> queued message -> the worker -> stored snapshot -> the page.  Only
RabbitMQ and Grad Café are faked.  The buttons publish through the real
web/publisher.publish_task, whose broker connection is the FakeBroker of
test_publisher.py: it keeps every message body that would have gone over the
wire.  Stack.run_worker() then hands those exact bytes, in the order they were
queued, to the real worker (worker/consumer.Worker.process_message), which
runs each task in one transaction on the *_test database and acks it.  The
scraper reads the fake Grad Café (worker_site), and the page reads the stored
snapshot back through the real default QUERY_FN.  So these tests show the
pieces work together, not one at a time.
"""

from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup

from db.load_data import fetch_applicants
from fake_gradcafe import ROBOTS_TXT, ROBOTS_URL, applicant, listing_page, page, survey_url
from test_consumer import Answers, delivery
from test_publisher import broker  # noqa: F401  (fixture: publish_task's fake RabbitMQ)
from web.app import create_app
from worker.consumer import Worker
from worker.etl.ingest import insert_scraped_entries


class Stack:
    """The web app with all its real defaults, and the worker behind the fake broker."""

    def __init__(self, client, fake_broker) -> None:
        self.client = client
        self.broker = fake_broker
        self.channel = Answers()            # records the worker's ack / nack of each message
        self.delivered = 0

    def messages(self) -> list[bytes]:
        """Every message body the buttons have published so far, in order."""
        return [connection.fake_channel.published()["body"] for connection in self.broker.connections]

    def kinds(self) -> list[str]:
        return [json.loads(raw)["kind"] for raw in self.messages()]

    def run_worker(self) -> None:
        """Deliver every message not delivered yet to the worker, one at a time."""
        worker = Worker()
        for raw in self.messages()[self.delivered:]:
            self.delivered += 1
            worker.process_message(self.channel, delivery(self.delivered), None, raw)


@pytest.fixture
def stack(db_conn, broker, worker_site):  # noqa: F811  (broker is the imported fixture)
    return Stack(create_app().test_client(), broker)


def fall_2026_applicant(result_id, citizenship, gpa, decision) -> dict:
    """A Fall 2026 listing row carrying the fields the analysis questions count."""
    return applicant(result_id, tags=["Fall 2026", citizenship, f"GPA {gpa}"],
                     decision=f"{decision} on Sep 18")


def serve_listing(site, rows: list[dict]) -> None:
    """Grad Café's first listing page shows *rows* (newest first), and nothing follows it."""
    site.serve(ROBOTS_URL, page(ROBOTS_TXT))
    site.serve(survey_url(), page(listing_page(rows)))


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


def entries_shown(response) -> str | None:
    """The "entries in the database" number at the top of the page (None: no analysis yet)."""
    stat = BeautifulSoup(response.data, "html.parser").select_one(".stat-value")
    return stat.get_text(strip=True) if stat else None


FIRST_BATCH = [
    fall_2026_applicant(9_100_003, "American", "3.70", "Accepted"),
    fall_2026_applicant(9_100_002, "American", "3.50", "Rejected"),
    fall_2026_applicant(9_100_001, "International", "3.90", "Accepted"),
]


@pytest.mark.integration
def test_pull_update_render(stack, worker_site):
    before = stack.client.get("/analysis")
    assert entries_shown(before) is None                 # nothing computed yet...
    assert "No analysis has been computed yet" in before.get_data(as_text=True)
    serve_listing(worker_site, FIRST_BATCH)

    pull = stack.client.post("/pull-data")
    stack.run_worker()
    update = stack.client.post("/update-analysis")
    stack.run_worker()
    after = stack.client.get("/analysis")

    assert (pull.status_code, update.status_code) == (202, 202)
    assert stack.kinds() == ["scrape_new_data", "recompute_analytics"]
    assert stack.channel.answers == [("ack", 1), ("ack", 2)]   # each acked after its commit
    assert after.status_code == 200
    assert entries_shown(after) == "3"
    results = page_results(after)
    assert results[("1", "Fall 2026 applicant count")] == "3"
    assert results[("2", "Percent international")] == "33.33%"        # 1 of 3, two decimals
    assert results[("4", "Average GPA of American Fall 2026 applicants")] == "3.60"
    assert results[("6", "Average GPA of accepted Fall 2026 applicants")] == "3.80"


@pytest.mark.integration
def test_overlapping_pulls_keep_rows_unique(stack, db_conn, worker_site):
    serve_listing(worker_site, FIRST_BATCH)
    stack.client.post("/pull-data")
    stack.run_worker()
    # One new entry appeared above the three already stored.
    newest = fall_2026_applicant(9_100_004, "International", "3.80", "Accepted")
    serve_listing(worker_site, [newest] + FIRST_BATCH)

    stack.client.post("/pull-data")
    stack.run_worker()
    after = stack.client.get("/analysis")                 # the pull refreshed the snapshot itself

    p_ids = [row["p_id"] for row in fetch_applicants(db_conn)]
    assert p_ids == [9_100_004, 9_100_003, 9_100_002, 9_100_001]
    assert entries_shown(after) == "4"
    assert page_results(after)[("2", "Percent international")] == "50.00%"     # 2 of 4


@pytest.mark.integration
def test_the_page_changes_only_when_the_worker_has_stored_a_new_snapshot(stack, db_conn, worker_site,
                                                                          entry_factory):
    serve_listing(worker_site, FIRST_BATCH[2:])
    stack.client.post("/pull-data")
    stack.run_worker()
    first_status = stack.client.get("/api/analysis-status").get_json()

    # Another transaction is still running: rows are in, the snapshot is not yet.
    with db_conn.transaction():
        insert_scraped_entries(db_conn, [entry_factory(9_100_002), entry_factory(9_100_003)])
        assert entries_shown(stack.client.get("/analysis")) == "1"
        assert stack.client.get("/api/analysis-status").get_json() == first_status

    stack.client.post("/update-analysis")
    stack.run_worker()
    second_status = stack.client.get("/api/analysis-status").get_json()

    assert first_status["total_entries"] == 1
    assert second_status["total_entries"] == 3
    assert second_status["computed_at"] > first_status["computed_at"]   # what the page's poll waits for
    assert entries_shown(stack.client.get("/analysis")) == "3"
