"""
test_publisher.py - web/publisher.py, checked without a RabbitMQ broker.

pika.BlockingConnection is replaced by FakeConnection, which records every
call the publisher makes on the connection and on its channel; pika's real
URLParameters and BasicProperties are kept, so the tests see exactly what
would go over the wire.  No test opens a socket.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pika
import pika.exceptions
import pytest

from web import publisher
from web.app import create_app

pytestmark = pytest.mark.web

BROKER_URL = "amqp://tasks_user:not-a-real-password@broker.test:5672/%2F"


class FakeChannel:
    """Records each call as (method name, keyword arguments); raises *fail_with* from the
    method named *fail_on*, the way a broker error surfaces in pika."""

    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str, **kwargs) -> None:
        self.calls.append((name, kwargs))
        broker = self.connection.broker
        if broker.fail_on == name:
            if broker.connection_lost:
                self.connection.is_open = False        # the error also closed the connection
            raise broker.fail_with

    def exchange_declare(self, **kwargs):
        self._record("exchange_declare", **kwargs)

    def queue_declare(self, **kwargs):
        self._record("queue_declare", **kwargs)

    def queue_bind(self, **kwargs):
        self._record("queue_bind", **kwargs)

    def confirm_delivery(self):
        self._record("confirm_delivery")

    def basic_publish(self, **kwargs):
        self._record("basic_publish", **kwargs)

    def names(self) -> list[str]:
        return [name for name, _kwargs in self.calls]

    def published(self) -> dict:
        """The keyword arguments of the one basic_publish call."""
        (kwargs,) = [kwargs for name, kwargs in self.calls if name == "basic_publish"]
        return kwargs


class FakeConnection:
    """Stands in for pika.BlockingConnection(params)."""

    def __init__(self, broker: "FakeBroker", params) -> None:
        self.broker = broker
        self.params = params
        self.is_open = True
        self.close_calls = 0
        self.fake_channel = FakeChannel(self)

    def channel(self) -> FakeChannel:
        return self.fake_channel

    def close(self) -> None:
        if not self.is_open:                            # pika raises here, too
            raise pika.exceptions.ConnectionWrongStateError("already closed")
        self.close_calls += 1
        self.is_open = False


class FakeBroker:
    """What the fake connections should do, and every connection that was opened."""

    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.refuse_connections: Exception | None = None
        self.fail_on: str | None = None
        self.fail_with: Exception = pika.exceptions.ChannelClosedByBroker(404, "NOT_FOUND")
        self.connection_lost = False

    def connect(self, params) -> FakeConnection:
        if self.refuse_connections is not None:
            raise self.refuse_connections
        self.connections.append(FakeConnection(self, params))
        return self.connections[-1]

    @property
    def channel(self) -> FakeChannel:
        (connection,) = self.connections
        return connection.fake_channel


@pytest.fixture
def broker(monkeypatch) -> FakeBroker:
    """RABBITMQ_URL set to a made-up broker, and pika.BlockingConnection replaced by FakeBroker."""
    fake = FakeBroker()
    monkeypatch.setenv("RABBITMQ_URL", BROKER_URL)
    monkeypatch.setattr(publisher.pika, "BlockingConnection", fake.connect)
    return fake


# --------------------------------------------------------------------------- #
#                         _open_channel: the declarations                     #
# --------------------------------------------------------------------------- #

def test_the_names_are_the_ones_the_worker_consumes_from():
    assert (publisher.EXCHANGE, publisher.QUEUE, publisher.ROUTING_KEY) == ("tasks", "tasks_q", "tasks")


def test_open_channel_connects_to_rabbitmq_url_and_declares_durable_entities(broker):
    conn, channel = publisher._open_channel()

    assert (conn.params.host, conn.params.port, conn.params.virtual_host) == ("broker.test", 5672, "/")
    assert channel.calls == [
        ("exchange_declare", {"exchange": "tasks", "exchange_type": "direct", "durable": True}),
        ("queue_declare", {"queue": "tasks_q", "durable": True}),
        ("queue_bind", {"exchange": "tasks", "queue": "tasks_q", "routing_key": "tasks"}),
        ("confirm_delivery", {}),                       # publisher confirms are on
    ]
    assert conn.is_open                                 # the caller closes it


def test_a_publish_waits_at_most_ten_seconds_while_the_broker_blocks_publishers(broker):
    # During a memory or disk alarm RabbitMQ blocks publishing connections; without this
    # timeout basic_publish would wait for its confirm, and the request with it, for ever.
    conn, _channel = publisher._open_channel()

    assert conn.params.blocked_connection_timeout == publisher.BLOCKED_TIMEOUT_SECONDS == 10


def test_a_rabbitmq_url_may_set_its_own_blocked_timeout(broker, monkeypatch):
    monkeypatch.setenv("RABBITMQ_URL", BROKER_URL + "?blocked_connection_timeout=3")

    conn, _channel = publisher._open_channel()

    assert conn.params.blocked_connection_timeout == 3


def test_a_failed_declaration_closes_the_connection_and_raises(broker):
    broker.fail_on = "queue_declare"

    with pytest.raises(pika.exceptions.ChannelClosedByBroker):
        publisher._open_channel()

    assert broker.connections[0].close_calls == 1


# --------------------------------------------------------------------------- #
#                         publish_task: the message                           #
# --------------------------------------------------------------------------- #

def test_publish_task_sends_one_compact_persistent_json_message(broker):
    before = datetime.now(timezone.utc)

    publisher.publish_task("recompute_analytics", {"since": 1020481}, {"requested_by": "test"})

    sent = broker.channel.published()
    assert (sent["exchange"], sent["routing_key"], sent["mandatory"]) == ("tasks", "tasks", False)
    message = json.loads(sent["body"].decode("utf-8"))
    assert set(message) == {"kind", "ts", "payload"}
    assert message["kind"] == "recompute_analytics"
    assert message["payload"] == {"since": 1020481}
    assert sent["body"] == json.dumps(message, separators=(",", ":")).encode("utf-8")   # compact
    assert b" " not in sent["body"]
    ts = datetime.fromisoformat(message["ts"])
    assert ts.utcoffset() == timedelta(0)               # timezone-aware UTC...
    assert before <= ts <= datetime.now(timezone.utc)   # ...and taken when the task was queued
    properties = sent["properties"]
    assert properties.delivery_mode == 2                # persistent: survives a broker restart
    assert properties.headers == {"requested_by": "test"}
    assert properties.content_type == "application/json"
    assert broker.connections[0].close_calls == 1       # closed after publishing


def test_payload_and_headers_default_to_empty(broker):
    publisher.publish_task("scrape_new_data")

    sent = broker.channel.published()
    assert json.loads(sent["body"])["payload"] == {}
    assert sent["properties"].headers == {}


def test_a_failed_publish_still_closes_the_connection_and_raises(broker):
    broker.fail_on = "basic_publish"
    broker.fail_with = pika.exceptions.NackError([])   # the broker refused the message

    with pytest.raises(pika.exceptions.NackError):
        publisher.publish_task("scrape_new_data")

    assert broker.connections[0].close_calls == 1      # the finally: block ran


def test_a_connection_the_error_already_closed_is_not_closed_twice(broker):
    broker.fail_on = "basic_publish"
    broker.fail_with = pika.exceptions.StreamLostError("connection reset")
    broker.connection_lost = True

    # The original error comes out, not pika's "already closed" from a second close().
    with pytest.raises(pika.exceptions.StreamLostError):
        publisher.publish_task("scrape_new_data")

    assert broker.connections[0].close_calls == 0


def test_an_unreachable_broker_raises(broker):
    broker.refuse_connections = pika.exceptions.AMQPConnectionError("connection refused")

    with pytest.raises(pika.exceptions.AMQPConnectionError):
        publisher.publish_task("scrape_new_data")

    assert broker.connections == []


@pytest.mark.parametrize(
    "url",
    [None, "not a url", "amqp://tasks_user@broker.test:5672/", "amqp://u:p@broker.test:port/"],
    ids=["unset", "unparseable", "user without a password", "port not a number"],
)
def test_a_missing_or_unusable_rabbitmq_url_raises_a_publish_error(broker, monkeypatch, url):
    if url is None:
        monkeypatch.delenv("RABBITMQ_URL")
    else:
        monkeypatch.setenv("RABBITMQ_URL", url)

    with pytest.raises(publisher.PUBLISH_ERRORS):
        publisher.publish_task("scrape_new_data")

    assert broker.connections == []                     # it never got as far as connecting


# --------------------------------------------------------------------------- #
#               The Flask app's default PUBLISH_FN is publish_task            #
# --------------------------------------------------------------------------- #

@pytest.mark.buttons
def test_the_buttons_publish_through_publish_task_by_default(broker):
    client = create_app(query_fn=lambda: None).test_client()

    response = client.post("/update-analysis")

    assert response.status_code == 202
    assert json.loads(broker.channel.published()["body"])["kind"] == "recompute_analytics"


@pytest.mark.buttons
def test_a_rabbitmq_url_without_a_password_is_a_503_that_does_not_echo_it(broker, monkeypatch, caplog):
    # pika itself raises TypeError for this URL; the publisher reports it as a ValueError.
    monkeypatch.setenv("RABBITMQ_URL", "amqp://tasks_user@broker.test:5672/")
    client = create_app(query_fn=lambda: None).test_client()

    response = client.post("/pull-data")

    assert response.status_code == 503
    assert "broker.test" not in response.get_data(as_text=True) + caplog.text


@pytest.mark.buttons
def test_a_publish_the_blocked_broker_timed_out_reaches_the_browser_as_503(broker):
    broker.fail_on = "basic_publish"
    broker.fail_with = pika.exceptions.ConnectionBlockedTimeout()   # an AMQPConnectionError
    broker.connection_lost = True                                    # pika closes it first
    client = create_app(query_fn=lambda: None).test_client()

    response = client.post("/update-analysis")

    assert response.status_code == 503
    assert broker.connections[0].close_calls == 0


@pytest.mark.buttons
def test_a_broker_outage_reaches_the_browser_as_503(broker):
    broker.refuse_connections = pika.exceptions.AMQPConnectionError("connection refused")
    client = create_app(query_fn=lambda: None).test_client()

    response = client.post("/pull-data")

    assert response.status_code == 503
    assert response.get_json()["queued"] is False
