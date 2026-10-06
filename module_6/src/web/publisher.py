"""
publisher.py - Hand work to the worker through RabbitMQ instead of doing it in a request.

The two buttons on the analysis page used to run their work inside the HTTP
request.  Now each click only publishes a small task message and the request
returns at once (HTTP 202); the worker service (worker/consumer.py) takes the
messages off the queue one at a time and does the work against PostgreSQL.

The AMQP layout, declared by both sides (declaring is idempotent: the first
declaration creates the entity, the next ones only confirm it exists)::

    exchange "tasks" (direct, durable) --routing key "tasks"--> queue "tasks_q" (durable)

Durable entities survive a broker restart, and every message is published
persistent (delivery_mode=2), so a queued task is not lost if RabbitMQ restarts
before the worker gets to it.  Publisher confirms are on: basic_publish returns
only after the broker has taken responsibility for the message, and raises if
it refuses it.  While RabbitMQ blocks publishers (a memory or disk alarm), a
publish waits at most BLOCKED_TIMEOUT_SECONDS and then raises, so the button
gets its 503 instead of hanging.

A message body is compact JSON::

    {"kind":"recompute_analytics","ts":"2026-10-05T18:00:00.123456+00:00","payload":{}}

RABBITMQ_URL says where the broker is: an AMQP URL of the form
amqp://USER:PASSWORD@HOST:5672/ (in the Compose stack HOST is rabbitmq).
Because it holds the broker password it comes from the environment and is
never logged.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pika
import pika.exceptions

EXCHANGE = "tasks"
QUEUE = "tasks_q"
ROUTING_KEY = "tasks"
PERSISTENT = 2   # AMQP delivery_mode 2: the broker writes the message to disk
BLOCKED_TIMEOUT_SECONDS = 10   # the longest a publish waits while RabbitMQ blocks publishers

# What publish_task() raises when a task cannot be queued: pika's own errors (broker
# down, refused login, a NACKed message, a ConnectionBlockedTimeout), a socket error, a
# missing RABBITMQ_URL (KeyError) or one pika cannot parse (ValueError, see _parameters).
# The web routes turn exactly these into HTTP 503; anything else is a programming error
# and surfaces as one.
PUBLISH_ERRORS = (pika.exceptions.AMQPError, OSError, LookupError, ValueError)


def _parameters(url: str) -> pika.URLParameters:
    """pika's connection parameters for *url*; ValueError if pika cannot use it.

    pika reports an unusable URL in several ways: ValueError (a bad port),
    IndexError, and TypeError for a user name without a password
    (amqp://gradcafe@rabbitmq/).  All become one ValueError, whose message
    never repeats the URL, since a URL can hold a password.

    Unless the URL sets its own blocked_connection_timeout, a publish waits at
    most BLOCKED_TIMEOUT_SECONDS while RabbitMQ blocks publishers; pika then
    raises ConnectionBlockedTimeout, an AMQPConnectionError, and the route
    answers 503.
    """
    try:
        params = pika.URLParameters(url)
    except (TypeError, IndexError, ValueError):
        raise ValueError("RABBITMQ_URL is not a usable AMQP URL "
                         "(amqp://USER:PASSWORD@HOST:5672/)") from None
    if params.blocked_connection_timeout is None:
        params.blocked_connection_timeout = BLOCKED_TIMEOUT_SECONDS
    return params


def _close(conn) -> None:
    """Close *conn* unless it is already closed (closing twice would raise and hide the
    error that closed it)."""
    if conn.is_open:
        conn.close()


def _open_channel():
    """Connect to RABBITMQ_URL; returns (connection, channel) with the exchange, the queue
    and their binding declared and publisher confirms enabled.

    The caller must close the connection.  If a declaration fails, the
    connection is closed here before the error propagates.
    """
    url = os.environ["RABBITMQ_URL"]
    params = _parameters(url)
    conn = pika.BlockingConnection(params)
    try:
        ch = conn.channel()
        # Durable exchange and queue, bound with the routing key; idempotent, so safe
        # to repeat on every publish.
        ch.exchange_declare(exchange=EXCHANGE, exchange_type="direct", durable=True)
        ch.queue_declare(queue=QUEUE, durable=True)
        ch.queue_bind(exchange=EXCHANGE, queue=QUEUE, routing_key=ROUTING_KEY)
        ch.confirm_delivery()   # basic_publish now waits for the broker's ack
    except BaseException:
        _close(conn)
        raise
    return conn, ch


def publish_task(kind: str, payload: dict | None = None, headers: dict | None = None) -> None:
    """Queue one task for the worker: *kind* names it ("scrape_new_data",
    "recompute_analytics"), *payload* carries its arguments (default {}).

    The message is persistent and confirmed by the broker.  The connection is
    always closed.  Any failure raises (see PUBLISH_ERRORS), so the caller can
    answer 503 instead of pretending the task was queued.
    """
    body = json.dumps(
        {"kind": kind, "ts": datetime.now(timezone.utc).isoformat(), "payload": payload or {}},
        separators=(",", ":"),
    ).encode("utf-8")
    conn, ch = _open_channel()
    try:
        ch.basic_publish(
            exchange=EXCHANGE,
            routing_key=ROUTING_KEY,
            body=body,
            properties=pika.BasicProperties(
                delivery_mode=PERSISTENT,
                headers=headers or {},
                content_type="application/json",
            ),
            mandatory=False,
        )
    finally:
        _close(conn)
