"""
consumer.py - The worker: take tasks off RabbitMQ one at a time and run them against PostgreSQL.

    python -m worker.consumer        (the worker container's command)

The web app's two buttons publish small JSON messages (web/publisher.py); this
long-running process consumes them.  At start-up it

  1. connects to RabbitMQ at RABBITMQ_URL, retrying for a while if the broker is
     still starting or its host name does not resolve yet (CONNECT_ATTEMPTS
     tries, CONNECT_RETRY_SECONDS apart, each one logged);
  2. declares the same durable direct exchange "tasks", durable queue "tasks_q"
     and binding (routing key "tasks") as the publisher.  Declaring is
     idempotent, so whichever side starts first creates them;
  3. sets basic_qos(prefetch_count=1): the broker hands this worker one message
     at a time and sends the next only after the previous one is answered
     (backpressure: a burst of clicks waits in the queue, not in memory);
  4. consumes with manual acknowledgements (auto_ack=False).

Every message goes through Worker.process_message():

  * the body must be a JSON object {"kind": ..., "ts": ..., "payload": {...}}
    of at most MAX_BODY_BYTES whose kind is in the task map TASKS, which
    routes it to its handler;
  * the handler runs inside ONE database transaction on a fresh connection
    from DATABASE_URL (``with conn.transaction():``), so the new rows, the
    watermark and the analysis snapshot are committed together or not at all;
  * basic_ack is sent only after that commit succeeded;
  * everything else - a body that is too big, not JSON (or nested too deeply
    to read) or not an object, an unknown kind, a bad payload, or any error in
    the handler (the transaction is then rolled back) - is answered with
    basic_nack(requeue=False).  The message is dropped and logged, never put
    back, so a task that cannot succeed does not loop forever, and the worker
    itself keeps running.  No message can leave its delivery unanswered.

The handlers are idempotent (INSERT ... ON CONFLICT DO NOTHING, an UPSERTed
watermark, a recomputed snapshot), so a message delivered twice - RabbitMQ
redelivers an unacknowledged message when a worker stops halfway - does no
harm.  The log marks a delivery that RabbitMQ sends again as "(redelivered)".

A pull can take minutes (the scraper pauses between pages), and pika's
BlockingConnection answers the broker's heartbeats only while its own thread
is free.  So each message is processed in a thread of its own (on_message)
while the main thread keeps the connection alive; the thread hands its ack or
nack back with add_callback_threadsafe, because a pika connection may only be
used from the thread that opened it.  prefetch_count=1 means there is never
more than one such thread.

Settings come from the environment: RABBITMQ_URL (an AMQP URL of the form
amqp://USER:PASSWORD@HOST:5672/; in the Compose stack HOST is rabbitmq)
and DATABASE_URL (or DB_*, see db/db_config.py) naming the worker's
least-privilege role; SCRAPE_DATA_DIR is the scraper's writable folder (see
etl/incremental_scraper.py).  Neither URL is ever logged: both hold passwords.

Exit codes: 0 stopped (SIGTERM from ``docker stop``, or Ctrl-C), 1 RABBITMQ_URL
missing or unusable, 2 the broker stayed unreachable, 3 the broker connection
was lost while consuming (the container's restart policy starts a new worker,
and RabbitMQ redelivers whatever was not acknowledged).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from functools import partial

import pika
import pika.exceptions
import psycopg

from db.db_config import CONNECT_TIMEOUT_SECONDS, get_database_url
from db.load_data import get_watermark, newest_p_id, set_watermark
from worker.etl import incremental_scraper
from worker.etl.analytics import refresh_snapshot
from worker.etl.ingest import insert_scraped_entries

# The AMQP names, the same as web/publisher.py's (the worker image has no web package).
EXCHANGE = "tasks"
QUEUE = "tasks_q"
ROUTING_KEY = "tasks"
PREFETCH_COUNT = 1   # one unacknowledged message at a time

CONNECT_ATTEMPTS = 10          # tries to reach the broker at start-up...
CONNECT_RETRY_SECONDS = 3.0    # ...this far apart: about half a minute in all
MAX_P_ID = 2**31 - 1           # applicants.p_id is an INTEGER
MAX_BODY_BYTES = 64 * 1024     # a real task message is about 100 bytes

# What connecting to a broker that cannot be reached raises: pika's connection errors, or
# a plain socket error such as socket.gaierror when the host name does not resolve (in the
# stack: while the rabbitmq container is stopped or not created yet).
BROKER_UNREACHABLE = (pika.exceptions.AMQPConnectionError, OSError)

logger = logging.getLogger("gradcafe.worker")

Handler = Callable[[psycopg.Connection, dict], dict]


class TaskError(ValueError):
    """A message the worker will not run: too big, not JSON, not an object, an unknown kind or
    a bad payload.  Sending it again would fail the same way, so it is dropped (nack)."""


def _short(value: object, limit: int = 80) -> str:
    """repr(*value*) cut to *limit* characters, for log lines about untrusted input."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


# --------------------------------------------------------------------------- #
#                 Task handlers: (conn, payload) -> summary                   #
# --------------------------------------------------------------------------- #

def _check_fields(payload: dict, allowed: tuple[str, ...]) -> None:
    """Refuse a payload with fields the task does not know (a typo would otherwise be
    silently ignored)."""
    unknown = sorted(key for key in payload if key not in allowed)
    if unknown:
        raise TaskError(f"unexpected payload field(s) {_short(unknown)}")


def since_from_payload(payload: dict) -> int | None:
    """payload["since"]: a result id to fetch entries after instead of the stored
    watermark (a whole number from 0 to MAX_P_ID), or None when it is not given."""
    _check_fields(payload, ("since",))
    since = payload.get("since")
    if since is None:
        return None
    if isinstance(since, bool) or not isinstance(since, int) or not 0 <= since <= MAX_P_ID:
        raise TaskError(f"payload since must be a whole number from 0 to {MAX_P_ID}, "
                        f"not {_short(since)}")
    return since


def stored_watermark(conn: psycopg.Connection) -> int | None:
    """The Grad Cafe watermark (ingestion_watermarks.last_seen) as a result id, or None when
    no pull has recorded one yet.  A value that is not a number is logged and treated as
    missing; the pull then replaces it."""
    text = get_watermark(conn)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        logger.warning("ignoring a watermark that is not a result id: %s", _short(text))
        return None


def handle_scrape_new_data(conn: psycopg.Connection, payload: dict) -> dict:
    """Fetch the Grad Cafe entries newer than the watermark, store them, advance the watermark.

    Runs inside the caller's transaction (one per message):

      1. last_seen = payload["since"] when given, else the stored watermark,
         else (no pull has recorded one yet) the newest p_id already stored;
      2. the scraper fetches only entries with a higher result id
         (etl/incremental_scraper.py: robots.txt and the 50-page cap apply);
      3. they are cleaned and inserted with INSERT ... ON CONFLICT (p_id) DO
         NOTHING (etl/ingest.py), so a repeated entry is skipped;
      4. the watermark is UPSERTed to the highest result id seen: the newest
         entry fetched, or where the stored data already ended if nothing was
         new.  It never moves backwards, and ``since`` alone never moves it;
      5. the analysis snapshot is recomputed, so the page shows the new rows,
         and its poll sees that the pull finished even when nothing was new.

    Any failure rolls all of it back.  Returns a summary for the log.
    """
    since = since_from_payload(payload)
    stored = stored_watermark(conn)
    floor = stored if stored is not None else newest_p_id(conn)   # where the stored data ends
    last_seen = since if since is not None else floor
    batch = incremental_scraper.fetch_new_entries(last_seen)
    inserted = insert_scraped_entries(conn, batch.entries)
    watermark = max((value for value in (floor, batch.max_id) if value is not None), default=None)
    if watermark is not None:
        set_watermark(conn, str(watermark))
    snapshot = refresh_snapshot(conn)
    return {
        "last_seen": last_seen,
        "pages": batch.pages,
        "fetched": len(batch.entries),
        "inserted": inserted,
        "watermark": watermark,
        "complete": batch.complete,
        "total_entries": snapshot["summary"]["total_entries"],
    }


def handle_recompute_analytics(conn: psycopg.Connection, payload: dict) -> dict:
    """Recompute every analysis answer with SQL and store it as the snapshot the page shows
    (etl/analytics.py), inside the caller's transaction.  The payload carries nothing."""
    _check_fields(payload, ())
    snapshot = refresh_snapshot(conn)
    return {"total_entries": snapshot["summary"]["total_entries"]}


# The task map: a message's "kind" -> the handler that runs it.
TASKS: dict[str, Handler] = {
    "scrape_new_data": handle_scrape_new_data,
    "recompute_analytics": handle_recompute_analytics,
}


# --------------------------------------------------------------------------- #
#                                One message                                  #
# --------------------------------------------------------------------------- #

def parse_task(body: bytes, tasks: Mapping[str, Handler] | None = None) -> tuple[str, dict]:
    """(kind, payload) of one message body; TaskError if the worker cannot run it.  The kind
    must be one of *tasks* (default TASKS)."""
    tasks = TASKS if tasks is None else tasks
    if len(body) > MAX_BODY_BYTES:
        raise TaskError(f"the body is {len(body)} bytes, more than the {MAX_BODY_BYTES} allowed")
    try:
        message = json.loads(body)
    # ValueError: not JSON, or not UTF-8 (UnicodeDecodeError is a ValueError).
    # RecursionError: arrays or objects nested deeper than Python's recursion limit.
    except (ValueError, RecursionError) as err:
        raise TaskError("the body is not JSON the worker can read "
                        f"({type(err).__name__})") from err
    if not isinstance(message, dict):
        raise TaskError(f"expected a JSON object, got {type(message).__name__}")
    kind = message.get("kind")
    if not isinstance(kind, str) or kind not in tasks:
        raise TaskError(f"unknown task kind {_short(kind)}")
    payload = message.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise TaskError(f"the payload must be a JSON object, got {type(payload).__name__}")
    return kind, payload


def connect_database() -> psycopg.Connection:
    """A new autocommit connection from DATABASE_URL (or DB_*).  In autocommit mode a
    ``with conn.transaction():`` block is the whole transaction: it commits exactly when
    the block ends and rolls back if the block raises."""
    return psycopg.connect(get_database_url(), autocommit=True,
                           connect_timeout=CONNECT_TIMEOUT_SECONDS)


class ThreadSafeAcks:
    """basic_ack and basic_nack for a handler thread.  pika is not thread-safe, so each call
    is handed to the connection's own thread with add_callback_threadsafe, the one method
    that may be called from another thread."""

    def __init__(self, channel) -> None:
        self._channel = channel

    def basic_ack(self, delivery_tag: int) -> None:
        """Acknowledge *delivery_tag* from the connection's thread."""
        self._channel.connection.add_callback_threadsafe(
            partial(self._channel.basic_ack, delivery_tag=delivery_tag))

    def basic_nack(self, delivery_tag: int, requeue: bool = False) -> None:
        """Reject *delivery_tag* from the connection's thread."""
        self._channel.connection.add_callback_threadsafe(
            partial(self._channel.basic_nack, delivery_tag=delivery_tag, requeue=requeue))


class Worker:
    """Runs task messages: route by kind, one transaction each, then ack or nack.

    *tasks* (default TASKS) maps kinds to handlers and *connect* (default
    connect_database) opens the database connection for each message; tests
    pass their own of both.
    """

    def __init__(self, tasks: Mapping[str, Handler] | None = None,
                 connect: Callable[[], psycopg.Connection] = connect_database) -> None:
        self.tasks = dict(TASKS if tasks is None else tasks)
        self.connect = connect
        self.thread: threading.Thread | None = None   # the thread of the latest delivery

    def process_message(self, channel, method, _properties, body: bytes) -> bool:
        """Handle one delivery completely; True if it was acknowledged, False if rejected.

        *channel* only needs basic_ack and basic_nack (a pika channel, ThreadSafeAcks or
        a test's fake); *method* carries the delivery_tag and the redelivered flag.
        Whatever the body holds, the delivery ends in exactly one ack or nack.
        """
        tag = method.delivery_tag
        kind = None
        try:
            kind, payload = parse_task(body, self.tasks)
            logger.info("delivery %s: running %s%s, payload %s", tag, kind,
                        " (redelivered)" if method.redelivered else "", _short(payload))
            started = time.monotonic()
            with self.connect() as conn, conn.transaction():
                summary = self.tasks[kind](conn, payload)
        # Whatever failed - the body, the payload, the scraper, the database, or a bug - any
        # transaction has been rolled back, and the delivery must still be answered: an
        # unanswered message would hold the worker's only prefetch slot forever.
        except Exception as err:  # pylint: disable=broad-exception-caught
            expected = isinstance(err, TaskError)       # a refused message needs no traceback
            if kind is None:
                logger.warning("delivery %s refused: %s", tag, err, exc_info=not expected)
            else:
                logger.error("delivery %s: %s failed and was rolled back: %s: %s", tag, kind,
                             type(err).__name__, err, exc_info=not expected)
            return self._settle(channel, tag, ack=False)
        logger.info("delivery %s: %s committed in %.1f s: %s", tag, kind,
                    time.monotonic() - started, summary)
        return self._settle(channel, tag, ack=True)

    @staticmethod
    def _settle(channel, tag: int, ack: bool) -> bool:
        """basic_ack, or basic_nack without requeue.  If the broker connection is gone the
        answer cannot be sent; RabbitMQ then redelivers the message to the next worker."""
        try:
            if ack:
                channel.basic_ack(delivery_tag=tag)
            else:
                channel.basic_nack(delivery_tag=tag, requeue=False)
        except pika.exceptions.AMQPError as err:
            logger.error("delivery %s: could not send the %s (%s); RabbitMQ will redeliver it",
                         tag, "ack" if ack else "nack", type(err).__name__)
            return False
        logger.info("delivery %s %s", tag,
                    "acknowledged (basic_ack) after the commit" if ack
                    else "rejected (basic_nack, requeue=False)")
        return ack

    def on_message(self, channel, method, properties, body: bytes) -> None:
        """pika's on_message_callback: process the delivery in a new thread, so this thread
        (the connection's) keeps answering heartbeats while a long pull runs."""
        self.thread = threading.Thread(
            target=self.process_message,
            args=(ThreadSafeAcks(channel), method, properties, body),
            name=f"task-{method.delivery_tag}",
            daemon=True,
        )
        self.thread.start()

    def consume(self, channel) -> None:
        """Consume QUEUE with manual acknowledgements until the process is stopped."""
        channel.basic_consume(queue=QUEUE, on_message_callback=self.on_message, auto_ack=False)
        logger.info("waiting for tasks on queue %s (prefetch_count=%d)", QUEUE, PREFETCH_COUNT)
        channel.start_consuming()


# --------------------------------------------------------------------------- #
#                                 The broker                                  #
# --------------------------------------------------------------------------- #

def open_connection(params: pika.URLParameters, attempts: int = CONNECT_ATTEMPTS,
                    wait_seconds: float = CONNECT_RETRY_SECONDS) -> pika.BlockingConnection:
    """Connect to RabbitMQ, retrying while it is unreachable (BROKER_UNREACHABLE); after
    *attempts* failures the last error is raised."""
    attempt = 1
    while True:
        try:
            return pika.BlockingConnection(params)
        except BROKER_UNREACHABLE as err:
            if attempt >= attempts:
                raise
            logger.warning("RabbitMQ is not reachable yet (attempt %d of %d, %s); "
                           "retrying in %.0f s", attempt, attempts, type(err).__name__,
                           wait_seconds)
        attempt += 1
        time.sleep(wait_seconds)


def setup_channel(connection: pika.BlockingConnection):
    """A channel with the durable exchange, queue and binding declared (idempotent) and
    basic_qos(prefetch_count=1) set."""
    channel = connection.channel()
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="direct", durable=True)
    channel.queue_declare(queue=QUEUE, durable=True)
    channel.queue_bind(exchange=EXCHANGE, queue=QUEUE, routing_key=ROUTING_KEY)
    channel.basic_qos(prefetch_count=PREFETCH_COUNT)
    return channel


def _stop(_signum, _frame) -> None:
    """SIGTERM (``docker stop``) ends the worker the same way Ctrl-C does."""
    raise KeyboardInterrupt


def _close(connection: pika.BlockingConnection) -> None:
    """Close *connection* if it is still open; a broker that is already gone is ignored."""
    try:
        if connection.is_open:
            connection.close()
    except pika.exceptions.AMQPError:
        pass


def run(params: pika.URLParameters, worker: Worker) -> int:
    """Connect, declare and consume until stopped; returns the exit code (see the module
    docstring)."""
    try:
        connection = open_connection(params)
    except BROKER_UNREACHABLE as err:
        logger.error("giving up: RabbitMQ stayed unreachable (%s)", type(err).__name__)
        return 2
    logger.info("connected to RabbitMQ")
    try:
        worker.consume(setup_channel(connection))
    except pika.exceptions.AMQPError as err:
        logger.error("lost the RabbitMQ connection (%s); exiting so a new worker can start",
                     type(err).__name__)
        return 3
    finally:
        _close(connection)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command line: run the worker until it is stopped; returns the exit code."""
    parser = argparse.ArgumentParser(
        description="Consume scrape_new_data and recompute_analytics tasks from RabbitMQ "
                    "and run them against PostgreSQL, one transaction per message.",
        epilog="Settings: RABBITMQ_URL (amqp://USER:PASSWORD@HOST:5672/), "
               "DATABASE_URL or DB_* for the worker's database role, SCRAPE_DATA_DIR for the "
               "scraper's files.",
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # pika logs each failed connection attempt at ERROR, with a traceback; the worker already
    # logs every broker failure in one line of its own (open_connection, run).
    logging.getLogger("pika").setLevel(logging.CRITICAL)

    url = os.environ.get("RABBITMQ_URL", "").strip()
    try:
        if not url:
            raise ValueError("RABBITMQ_URL is not set")
        params = pika.URLParameters(url)
    # pika raises ValueError, IndexError or (for a user without a password) TypeError for an
    # unusable URL.  The URL holds a password: never echo it.
    except (LookupError, ValueError, TypeError):
        logger.error("RABBITMQ_URL is missing or is not an AMQP URL "
                     "(amqp://USER:PASSWORD@HOST:5672/)")
        return 1

    signal.signal(signal.SIGTERM, _stop)
    try:
        return run(params, Worker())
    except KeyboardInterrupt:
        logger.info("stopped (a task still running, if any, was not acknowledged, so RabbitMQ "
                    "delivers it again)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
