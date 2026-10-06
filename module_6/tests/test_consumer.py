"""
test_consumer.py - worker/consumer.py, the RabbitMQ consumer, checked without a broker.

Worker.process_message() is called directly with a channel stand-in that
records every basic_ack and basic_nack, so each test sees exactly how a
delivery was answered.  The database is the real *_test database: the tests
prove that an ack is sent only once the transaction has committed, and that a
failing task is rolled back.  pika.BlockingConnection is replaced by a fake for
the start-up tests (declarations, prefetch, retries, exit codes).  No test
opens a socket to RabbitMQ or the internet.
"""

from __future__ import annotations

import json
import logging
import runpy
import signal
import socket
import threading
from types import SimpleNamespace

import pika
import pika.exceptions
import psycopg
import pytest

from db.load_data import count_rows, get_watermark
from db.snapshot import snapshot_status
from fake_gradcafe import ROBOTS_URL, page
from test_db_hardening import throwaway_roles, worker_role  # noqa: F401  (fixtures used below)
from web import publisher
from web.app import routes
from worker import consumer
from worker.consumer import TaskError, Worker, parse_task
from worker.etl.ingest import insert_scraped_entries

pytestmark = pytest.mark.buttons

BROKER_URL = "amqp://tasks_user:not-a-real-password@broker.test:5672/%2F"


def body(kind: str, payload: dict | None = None) -> bytes:
    """A message body exactly as web/publisher.py builds it."""
    message = {"kind": kind, "ts": "2026-10-05T18:00:00+00:00", "payload": payload or {}}
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def delivery(tag: int = 1, redelivered: bool = False) -> SimpleNamespace:
    """The method frame pika passes to the callback; only delivery_tag and redelivered are used."""
    return SimpleNamespace(delivery_tag=tag, redelivered=redelivered)


class Answers:
    """Stands in for the channel: records ("ack", tag) and ("nack", tag, requeue) in order.

    on_ack, if set, runs at the moment basic_ack is called; fail_with makes both
    methods raise, the way pika does when the connection is gone.
    """

    def __init__(self) -> None:
        self.answers: list[tuple] = []
        self.on_ack = None
        self.fail_with: Exception | None = None

    def basic_ack(self, delivery_tag):
        if self.fail_with is not None:
            raise self.fail_with
        if self.on_ack is not None:
            self.on_ack()
        self.answers.append(("ack", delivery_tag))

    def basic_nack(self, delivery_tag, requeue=True):
        if self.fail_with is not None:
            raise self.fail_with
        self.answers.append(("nack", delivery_tag, requeue))


@pytest.fixture
def channel() -> Answers:
    return Answers()


def never_connect():
    pytest.fail("a message that cannot run must not even open a database connection")


# --------------------------------------------------------------------------- #
#                  The names and the task map match the web side               #
# --------------------------------------------------------------------------- #

def test_the_worker_consumes_where_the_web_app_publishes():
    assert (consumer.EXCHANGE, consumer.QUEUE, consumer.ROUTING_KEY) == \
        (publisher.EXCHANGE, publisher.QUEUE, publisher.ROUTING_KEY)


def test_the_task_map_routes_each_button_to_its_handler():
    assert consumer.TASKS == {
        "scrape_new_data": consumer.handle_scrape_new_data,
        "recompute_analytics": consumer.handle_recompute_analytics,
    }
    assert {routes.PULL_TASK, routes.UPDATE_TASK} == set(consumer.TASKS)   # what the buttons send


# --------------------------------------------------------------------------- #
#                           parse_task: the message body                       #
# --------------------------------------------------------------------------- #

def test_parse_task_returns_the_kind_and_payload():
    assert parse_task(body("scrape_new_data", {"since": 5})) == ("scrape_new_data", {"since": 5})


@pytest.mark.parametrize("raw", [b'{"kind":"recompute_analytics"}',
                                 b'{"kind":"recompute_analytics","payload":null}'])
def test_a_missing_payload_means_an_empty_one(raw):
    assert parse_task(raw) == ("recompute_analytics", {})


MALFORMED = {
    "not JSON": b"this is not json",
    "not UTF-8": b"\x80\x81\x82",
    "empty body": b"",
    "a list": b'["scrape_new_data"]',
    "a string": b'"scrape_new_data"',
    "no kind": b'{"payload":{}}',
    "unknown kind": b'{"kind":"drop_all_tables","payload":{}}',
    "unhashable kind": b'{"kind":["scrape_new_data"],"payload":{}}',
    "payload not an object": b'{"kind":"recompute_analytics","payload":[1,2]}',
    # json.loads raises RecursionError, not ValueError, for arrays nested this deep.
    "nested too deeply": b"[" * 50_000,
    "payload nested too deeply": b'{"kind":"recompute_analytics","payload":' + b"[" * 50_000,
    # Valid JSON, but far bigger than any real task message.
    "too big": b'{"kind":"recompute_analytics","payload":{}}' + b" " * consumer.MAX_BODY_BYTES,
}


@pytest.mark.parametrize("raw", MALFORMED.values(), ids=MALFORMED.keys())
def test_parse_task_refuses_what_the_worker_cannot_run(raw):
    with pytest.raises(TaskError):
        parse_task(raw)


# --------------------------------------------------------------------------- #
#               process_message: one transaction, then ack or nack             #
# --------------------------------------------------------------------------- #

@pytest.mark.db
def test_the_ack_is_sent_only_after_the_transaction_committed(db_conn, channel, entry_factory):
    with db_conn.transaction():
        insert_scraped_entries(db_conn, [entry_factory(1), entry_factory(2)])
    committed_at_ack = []
    # db_conn is a different connection: it sees the snapshot only once it is committed.
    channel.on_ack = lambda: committed_at_ack.append(snapshot_status(db_conn))

    acked = Worker().process_message(channel, delivery(7), None, body("recompute_analytics"))

    assert acked is True
    assert channel.answers == [("ack", 7)]
    assert committed_at_ack[0] is not None, "basic_ack was sent before the transaction committed"
    assert committed_at_ack[0]["total_entries"] == 2


@pytest.mark.db
def test_a_failing_task_is_rolled_back_and_nacked_without_requeue(db_conn, channel, entry_factory,
                                                                  caplog):
    written = []

    def write_then_fail(conn, _payload):
        consumer.set_watermark(conn, "1")                   # a plain UPSERT, first...
        written.append(insert_scraped_entries(conn, [entry_factory(1)]))
        written.append(count_rows(conn))                    # ...then the row, seen inside
        raise RuntimeError("the handler broke halfway")

    worker = Worker(tasks={"recompute_analytics": write_then_fail})

    with caplog.at_level(logging.ERROR, logger="gradcafe.worker"):
        acked = worker.process_message(channel, delivery(3), None, body("recompute_analytics"))

    assert acked is False
    assert channel.answers == [("nack", 3, False)]          # dropped, never requeued
    assert written == [1, 1]                                # both writes really happened...
    assert count_rows(db_conn) == 0                         # ...but the insert was rolled back
    assert get_watermark(db_conn) is None                   # ...and so was the watermark
    assert "rolled back" in caplog.text and "Traceback" in caplog.text


@pytest.mark.parametrize("raw", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_message_is_nacked_without_requeue_and_the_worker_goes_on(channel, raw, caplog):
    worker = Worker(connect=never_connect)

    with caplog.at_level(logging.WARNING, logger="gradcafe.worker"):
        acked = worker.process_message(channel, delivery(4), None, raw)

    assert acked is False
    assert channel.answers == [("nack", 4, False)]
    assert "refused" in caplog.text


def test_an_unexpected_error_before_the_task_starts_is_still_a_nack(channel, monkeypatch, caplog):
    def broken_parser(_body, _tasks):
        raise RuntimeError("a bug nobody foresaw")

    monkeypatch.setattr(consumer, "parse_task", broken_parser)

    with caplog.at_level(logging.WARNING, logger="gradcafe.worker"):
        acked = Worker(connect=never_connect).process_message(channel, delivery(6), None,
                                                              body("recompute_analytics"))

    assert (acked, channel.answers) == (False, [("nack", 6, False)])
    assert "refused" in caplog.text and "Traceback" in caplog.text   # a bug keeps its traceback


def test_unknown_kind_is_logged_without_echoing_a_long_body(channel, caplog):
    raw = json.dumps({"kind": "x" * 500}).encode()

    with caplog.at_level(logging.WARNING, logger="gradcafe.worker"):
        Worker(connect=never_connect).process_message(channel, delivery(), None, raw)

    assert "x" * 100 not in caplog.text                     # cut short in the log
    assert channel.answers == [("nack", 1, False)]


def test_a_database_outage_is_a_nack_not_a_crash(channel):
    def unreachable():
        raise psycopg.OperationalError("connection refused")

    acked = Worker(connect=unreachable).process_message(channel, delivery(5), None,
                                                        body("recompute_analytics"))

    assert (acked, channel.answers) == (False, [("nack", 5, False)])


@pytest.mark.db
def test_a_bad_payload_is_nacked_without_a_traceback(db_conn, channel, worker_site, caplog):
    with caplog.at_level(logging.ERROR, logger="gradcafe.worker"):
        Worker().process_message(channel, delivery(), None, body("scrape_new_data", {"since": -5}))

    assert channel.answers == [("nack", 1, False)]
    assert "TaskError" in caplog.text and "Traceback" not in caplog.text
    assert worker_site.requested == []


@pytest.mark.parametrize("answer", ["ack", "nack"])
def test_an_answer_the_broker_cannot_take_is_logged_and_the_worker_goes_on(channel, caplog, answer):
    channel.fail_with = pika.exceptions.ChannelWrongStateError("channel closed")
    tasks = {"recompute_analytics": lambda conn, payload: {}}
    connect = never_connect if answer == "nack" else FakeDatabase
    raw = b"not json" if answer == "nack" else body("recompute_analytics")

    with caplog.at_level(logging.ERROR, logger="gradcafe.worker"):
        acked = Worker(tasks=tasks, connect=connect).process_message(channel, delivery(), None, raw)

    assert acked is False
    assert f"could not send the {answer}" in caplog.text


class FakeDatabase:
    """A connection stand-in for tasks that never touch the database."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def transaction(self):
        return self


def test_a_redelivered_message_is_marked_in_the_log(channel, caplog):
    worker = Worker(tasks={"recompute_analytics": lambda conn, payload: {}}, connect=FakeDatabase)

    with caplog.at_level(logging.INFO, logger="gradcafe.worker"):
        worker.process_message(channel, delivery(1), None, body("recompute_analytics"))
        worker.process_message(channel, delivery(2, redelivered=True), None,
                               body("recompute_analytics"))

    running = [record.getMessage() for record in caplog.records if "running" in record.getMessage()]
    assert running == ["delivery 1: running recompute_analytics, payload {}",
                       "delivery 2: running recompute_analytics (redelivered), payload {}"]
    assert channel.answers == [("ack", 1), ("ack", 2)]


@pytest.mark.db
@pytest.mark.parametrize("fails", [False, True], ids=["committed", "rolled back"])
def test_each_message_closes_its_database_connection(database_url, channel, fails):
    opened = []

    def connect():
        opened.append(psycopg.connect(database_url, autocommit=True))
        return opened[-1]

    def task(_conn, _payload):
        if fails:
            raise RuntimeError("the handler broke")
        return {}

    Worker(tasks={"recompute_analytics": task}, connect=connect).process_message(
        channel, delivery(), None, body("recompute_analytics"))

    assert len(opened) == 1 and opened[0].closed            # the worker role has CONNECTION LIMIT 10
    assert channel.answers == ([("nack", 1, False)] if fails else [("ack", 1)])


# --------------------------------------------------------------------------- #
#                       The two tasks, as messages                             #
# --------------------------------------------------------------------------- #

@pytest.mark.db
def test_a_scrape_message_stores_new_entries_and_the_watermark(db_conn, channel, worker_site):
    worker_site.serve_listing([[12, 11], [10]])

    acked = Worker().process_message(channel, delivery(), None, body("scrape_new_data"))

    assert acked is True
    assert count_rows(db_conn) == 3
    assert get_watermark(db_conn) == "12"
    assert snapshot_status(db_conn)["total_entries"] == 3


@pytest.mark.db
def test_a_scrape_blocked_by_robots_txt_is_nacked_and_writes_nothing(db_conn, channel, worker_site):
    worker_site.serve(ROBOTS_URL, page("User-agent: *\nDisallow: /\n"))

    acked = Worker().process_message(channel, delivery(), None, body("scrape_new_data"))

    assert (acked, channel.answers) == (False, [("nack", 1, False)])
    assert (count_rows(db_conn), get_watermark(db_conn), snapshot_status(db_conn)) == (0, None, None)


@pytest.mark.db
def test_recompute_analytics_stores_the_snapshot(db_conn, entry_factory):
    with db_conn.transaction():
        insert_scraped_entries(db_conn, [entry_factory(1)])
        summary = consumer.handle_recompute_analytics(db_conn, {})

    assert summary == {"total_entries": 1}
    assert snapshot_status(db_conn)["total_entries"] == 1


def test_recompute_analytics_refuses_payload_fields():
    with pytest.raises(TaskError, match="unexpected payload"):
        consumer.handle_recompute_analytics(None, {"force": True})   # refused before any SQL


@pytest.mark.db
def test_the_worker_role_has_every_privilege_both_tasks_need(db_conn, worker_role, worker_site, channel):
    worker_site.serve_listing([[22, 21]])
    worker = Worker(connect=lambda: psycopg.connect(worker_role.url, autocommit=True))

    scraped = worker.process_message(channel, delivery(1), None, body("scrape_new_data"))
    recomputed = worker.process_message(channel, delivery(2), None, body("recompute_analytics"))

    assert (scraped, recomputed) == (True, True)
    assert channel.answers == [("ack", 1), ("ack", 2)]
    assert (count_rows(db_conn), get_watermark(db_conn)) == (2, "22")


# --------------------------------------------------------------------------- #
#         on_message: the task runs in its own thread, the ack does not        #
# --------------------------------------------------------------------------- #

class FakeConnection:
    """A BlockingConnection stand-in: add_callback_threadsafe only queues the callback,
    the way pika queues it for the connection's own thread."""

    def __init__(self) -> None:
        self.callbacks: list = []
        self.is_open = True
        self.closed = 0

    def add_callback_threadsafe(self, callback) -> None:
        self.callbacks.append(callback)

    def run_callbacks(self) -> None:
        """What the connection's thread does on its next turn of the event loop."""
        while self.callbacks:
            self.callbacks.pop(0)()

    def close(self) -> None:
        self.closed += 1
        self.is_open = False


class FakeChannel(Answers):
    """A channel of FakeConnection that also records the setup calls and the consumer."""

    def __init__(self, connection: FakeConnection) -> None:
        super().__init__()
        self.connection = connection
        self.calls: list[tuple[str, dict]] = []
        self.deliveries: list[bytes] = []          # what start_consuming will deliver
        self.consumer = None
        self.stop_with: BaseException = KeyboardInterrupt()

    def exchange_declare(self, **kwargs):
        self.calls.append(("exchange_declare", kwargs))

    def queue_declare(self, **kwargs):
        self.calls.append(("queue_declare", kwargs))

    def queue_bind(self, **kwargs):
        self.calls.append(("queue_bind", kwargs))

    def basic_qos(self, **kwargs):
        self.calls.append(("basic_qos", kwargs))

    def basic_consume(self, **kwargs):
        self.calls.append(("basic_consume", kwargs))
        self.consumer = kwargs["on_message_callback"]

    def start_consuming(self):
        """Deliver each queued body, wait for its thread, run the callbacks it handed
        back, then stop the way SIGTERM stops the real worker."""
        for tag, raw in enumerate(self.deliveries, start=1):
            self.consumer(self, delivery(tag), None, raw)
            self.consumer.__self__.thread.join(timeout=30)
            self.connection.run_callbacks()
        raise self.stop_with


def test_on_message_runs_the_task_in_another_thread_and_acks_from_the_connection_thread():
    connection = FakeConnection()
    channel = FakeChannel(connection)
    ran_in = []
    worker = Worker(tasks={"recompute_analytics": lambda conn, payload: ran_in.append(
        threading.current_thread()) or {}}, connect=FakeDatabase)

    worker.on_message(channel, delivery(9), None, body("recompute_analytics"))
    worker.thread.join(timeout=30)

    assert ran_in == [worker.thread] and worker.thread is not threading.main_thread()
    assert channel.answers == []                         # nothing sent from the task's thread...
    connection.run_callbacks()
    assert channel.answers == [("ack", 9)]               # ...only from the connection's thread


@pytest.mark.parametrize("raw", [b"not json", MALFORMED["payload nested too deeply"]],
                         ids=["not JSON", "nested too deeply"])
def test_a_refused_message_is_nacked_from_the_connection_thread_too(raw):
    # The deeply nested body once raised RecursionError out of the thread: no ack, no nack,
    # and with prefetch_count=1 the worker would have waited on that delivery for good.
    connection = FakeConnection()
    channel = FakeChannel(connection)
    worker = Worker(connect=never_connect)

    worker.on_message(channel, delivery(2), None, raw)
    worker.thread.join(timeout=30)
    connection.run_callbacks()

    assert channel.answers == [("nack", 2, False)]


# --------------------------------------------------------------------------- #
#             Start-up: declarations, prefetch, consume, retries               #
# --------------------------------------------------------------------------- #

def test_setup_declares_durable_entities_and_takes_one_message_at_a_time():
    connection = FakeConnection()
    connection.channel = lambda: FakeChannel(connection)

    channel = consumer.setup_channel(connection)

    assert channel.calls == [
        ("exchange_declare", {"exchange": "tasks", "exchange_type": "direct", "durable": True}),
        ("queue_declare", {"queue": "tasks_q", "durable": True}),
        ("queue_bind", {"exchange": "tasks", "queue": "tasks_q", "routing_key": "tasks"}),
        ("basic_qos", {"prefetch_count": 1}),
    ]


def test_consume_uses_manual_acknowledgements():
    channel = FakeChannel(FakeConnection())
    worker = Worker(connect=never_connect)

    with pytest.raises(KeyboardInterrupt):
        worker.consume(channel)

    assert channel.calls == [("basic_consume", {"queue": "tasks_q",
                                                "on_message_callback": worker.on_message,
                                                "auto_ack": False})]


class FakeBroker:
    """pika.BlockingConnection replaced: the first *failures* connections raise *error*.

    It also records the waits between retries (sleeps) and the signal handlers
    main() installs (signals), instead of really sleeping or installing them."""

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.error: Exception = pika.exceptions.AMQPConnectionError("connection refused")
        self.attempts = 0
        self.params = None
        self.sleeps: list[float] = []
        self.signals: list[tuple] = []
        self.connection = FakeConnection()
        self.channel = FakeChannel(self.connection)
        self.connection.channel = lambda: self.channel

    def connect(self, params):
        self.attempts += 1
        self.params = params
        if self.attempts <= self.failures:
            raise self.error
        return self.connection


@pytest.fixture
def broker(monkeypatch):
    """A fake broker at RABBITMQ_URL; sleeping between retries is recorded, not waited."""
    fake = FakeBroker()
    monkeypatch.setenv("RABBITMQ_URL", BROKER_URL)
    monkeypatch.setattr(consumer.pika, "BlockingConnection", fake.connect)
    monkeypatch.setattr(consumer.time, "sleep", fake.sleeps.append)
    monkeypatch.setattr(consumer.signal, "signal", lambda *args: fake.signals.append(args))
    return fake


def test_open_connection_retries_while_the_broker_is_starting(broker, caplog):
    broker.failures = 2

    with caplog.at_level(logging.WARNING, logger="gradcafe.worker"):
        connection = consumer.open_connection(pika.URLParameters(BROKER_URL))

    assert connection is broker.connection
    assert broker.attempts == 3
    assert broker.sleeps == [consumer.CONNECT_RETRY_SECONDS] * 2
    assert caplog.text.count("not reachable yet") == 2
    assert "not-a-real-password" not in caplog.text


def test_open_connection_also_retries_while_the_host_name_does_not_resolve(broker, caplog):
    # pika raises socket.gaierror, not an AMQPConnectionError, while the rabbitmq container is
    # stopped: its host name is then unknown on the Compose network.
    broker.failures = 1
    broker.error = socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    with caplog.at_level(logging.WARNING, logger="gradcafe.worker"):
        connection = consumer.open_connection(pika.URLParameters(BROKER_URL))

    assert connection is broker.connection
    assert broker.attempts == 2
    assert "not reachable yet (attempt 1 of 10, gaierror)" in caplog.text


def test_open_connection_gives_up_after_the_last_attempt(broker):
    broker.failures = 100

    with pytest.raises(pika.exceptions.AMQPConnectionError):
        consumer.open_connection(pika.URLParameters(BROKER_URL), attempts=4)

    assert (broker.attempts, len(broker.sleeps)) == (4, 3)


# --------------------------------------------------------------------------- #
#                     main(): the long-running process                         #
# --------------------------------------------------------------------------- #

@pytest.mark.db
def test_main_consumes_until_stopped_and_acks_each_message(db_conn, broker, worker_site):
    worker_site.serve_listing([[31, 30]])
    broker.channel.deliveries = [body("scrape_new_data"), b"garbage", body("recompute_analytics")]

    assert consumer.main([]) == 0                        # stopped by the (simulated) SIGTERM

    assert broker.channel.answers == [("ack", 1), ("nack", 2, False), ("ack", 3)]
    assert ("basic_qos", {"prefetch_count": 1}) in broker.channel.calls
    assert broker.params.host == "broker.test"
    assert broker.connection.closed == 1
    assert count_rows(db_conn) == 2


def test_main_installs_a_sigterm_handler_that_stops_like_ctrl_c(broker):
    consumer.main([])

    assert broker.signals == [(signal.SIGTERM, consumer._stop)]
    with pytest.raises(KeyboardInterrupt):
        consumer._stop(signal.SIGTERM, None)


@pytest.mark.parametrize("error", [pika.exceptions.AMQPConnectionError("connection refused"),
                                   socket.gaierror(socket.EAI_NONAME, "Name or service not known")],
                         ids=["refused", "unknown host"])
def test_main_exits_2_when_the_broker_never_answers(broker, caplog, error):
    broker.failures = 100
    broker.error = error

    assert consumer.main([]) == 2

    assert broker.attempts == consumer.CONNECT_ATTEMPTS
    assert f"stayed unreachable ({type(error).__name__})" in caplog.text


def test_main_keeps_pikas_own_connection_tracebacks_out_of_the_log(broker):
    consumer.main([])

    assert logging.getLogger("pika").level == logging.CRITICAL   # the worker logs failures itself


def test_main_exits_3_when_the_connection_is_lost(broker, caplog):
    broker.channel.stop_with = pika.exceptions.StreamLostError("connection reset")

    assert consumer.main([]) == 3

    assert "lost the RabbitMQ connection" in caplog.text
    assert broker.connection.closed == 1


def test_main_exits_0_if_consuming_ever_returns(broker, monkeypatch):
    monkeypatch.setattr(FakeChannel, "start_consuming", lambda self: None)

    assert consumer.main([]) == 0


def test_closing_a_connection_the_broker_already_dropped_is_quiet():
    connection = FakeConnection()

    def broken_close():
        raise pika.exceptions.StreamLostError("gone")

    connection.close = broken_close
    consumer._close(connection)                          # no exception
    connection.is_open = False
    consumer._close(connection)                          # nothing to close


@pytest.mark.parametrize("url", [None, "", "not a url", "amqp://tasks_user@broker.test:5672/"],
                         ids=["unset", "blank", "unparseable", "user without a password"])
def test_main_exits_1_without_a_usable_rabbitmq_url(broker, monkeypatch, caplog, url):
    if url is None:
        monkeypatch.delenv("RABBITMQ_URL")
    else:
        monkeypatch.setenv("RABBITMQ_URL", url)

    assert consumer.main([]) == 1

    assert broker.attempts == 0
    assert "RABBITMQ_URL is missing" in caplog.text
    assert "(amqp://USER:PASSWORD@HOST:5672/)" in caplog.text   # the form it needs, as a pattern


def test_python_m_worker_consumer_runs_main(monkeypatch):
    monkeypatch.setattr("sys.argv", ["consumer.py"])     # RABBITMQ_URL is not set (see conftest)

    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("worker.consumer", run_name="__main__")

    assert stopped.value.code == 1
