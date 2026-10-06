"""
tests/test_buttons.py

Covers: POST /pull-data and POST /update-analysis.  Neither button does the work
itself any more: each one publishes a task for the worker through RabbitMQ and
answers 202 Accepted at once, or 503 when the task could not be queued.  The
publisher is a fake here (conftest.FakePublisher), so no broker is needed.
"""

from __future__ import annotations

import logging

import pika.exceptions
import pytest

from web.app import create_app  # sys.path already has src/ on it, thanks to conftest.py


# ---------------------------------------------------------------------------
# 202: the task is queued and the request returns at once
# ---------------------------------------------------------------------------

@pytest.mark.buttons
def test_pull_data_queues_a_scrape_task_and_returns_202(client, fake_publisher):
    response = client.post("/pull-data")

    assert response.status_code == 202
    assert response.get_json() == {"ok": True, "queued": True, "kind": "scrape_new_data"}
    assert fake_publisher.kinds == ["scrape_new_data"]       # exactly one task, of the right kind


@pytest.mark.buttons
def test_update_analysis_queues_a_recompute_task_and_returns_202(client, fake_publisher):
    response = client.post("/update-analysis")

    assert response.status_code == 202
    assert response.get_json() == {"ok": True, "queued": True, "kind": "recompute_analytics"}
    assert fake_publisher.kinds == ["recompute_analytics"]


@pytest.mark.buttons
def test_clicking_again_queues_another_task_instead_of_answering_busy(client, fake_publisher):
    # The queue and the worker's prefetch=1 serialize the work, so there is no 409 any more.
    statuses = [client.post(url).status_code for url in ("/pull-data", "/pull-data", "/update-analysis")]

    assert statuses == [202, 202, 202]
    assert fake_publisher.kinds == ["scrape_new_data", "scrape_new_data", "recompute_analytics"]


@pytest.mark.buttons
def test_the_buttons_never_read_the_analysis(fake_publisher):
    queries = []
    app = create_app(publish_fn=fake_publisher, query_fn=lambda: queries.append("run") or {})

    app.test_client().post("/pull-data")
    app.test_client().post("/update-analysis")

    assert queries == []                                      # the worker does the work, not the request


# ---------------------------------------------------------------------------
# 503: the task could not be queued
# ---------------------------------------------------------------------------

@pytest.mark.buttons
@pytest.mark.parametrize("url, kind", [("/pull-data", "scrape_new_data"),
                                       ("/update-analysis", "recompute_analytics")])
@pytest.mark.parametrize("error", [
    pika.exceptions.AMQPConnectionError("amqp://admin:s3cret@rabbitmq:5672 refused"),
    ConnectionRefusedError("connection refused by rabbitmq:5672"),
    KeyError("RABBITMQ_URL"),
], ids=["broker-down", "socket-refused", "no-rabbitmq-url"])
def test_a_task_that_cannot_be_queued_returns_503(client, fake_publisher, url, kind, error, caplog):
    fake_publisher.error = error

    with caplog.at_level(logging.ERROR):
        response = client.post(url)

    assert response.status_code == 503
    body = response.get_json()
    assert body["ok"] is False and body["queued"] is False and body["kind"] == kind
    assert body["error"] == "the task could not be queued; try again in a minute"
    page = response.get_data(as_text=True)
    assert "s3cret" not in page and "rabbitmq" not in page    # no broker detail leaks out
    assert type(error).__name__ in caplog.text                # the log names the error class...
    assert "s3cret" not in caplog.text                        # ...but never its message


@pytest.mark.buttons
def test_a_programming_error_in_the_publisher_is_not_hidden_as_503(fake_publisher):
    fake_publisher.error = TypeError("a bug, not an outage")
    app = create_app(publish_fn=fake_publisher, query_fn=lambda: {})
    app.config["PROPAGATE_EXCEPTIONS"] = True

    with pytest.raises(TypeError):
        app.test_client().post("/pull-data")
