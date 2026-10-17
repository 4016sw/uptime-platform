"""RabbitMQ helpers shared by the scheduler, worker and alerter.

Topology:
  check_jobs      scheduler -> workers   (one message per URL to check)
  status_changes  workers   -> alerter   (only when a monitor goes up/down)

Each queue has a dead-letter queue (<name>.dlq). A message that can't be
processed is rejected and lands there for inspection instead of being
retried forever or silently dropped.
"""

import json
import logging
import time

import pika
from pika.adapters.blocking_connection import BlockingChannel

log = logging.getLogger(__name__)

CHECK_JOBS_QUEUE = "check_jobs"
STATUS_CHANGES_QUEUE = "status_changes"
DEAD_LETTER_EXCHANGE = "uptime.dlx"


def connect(url: str, attempts: int = 30, delay_seconds: float = 2.0) -> pika.BlockingConnection:
    """Connect with retries, since RabbitMQ may still be starting up."""
    params = pika.URLParameters(url)
    for attempt in range(1, attempts + 1):
        try:
            return pika.BlockingConnection(params)
        except pika.exceptions.AMQPConnectionError as exc:
            log.warning("RabbitMQ not reachable (attempt %d/%d): %s", attempt, attempts, exc)
            time.sleep(delay_seconds)
    raise RuntimeError(f"Could not connect to RabbitMQ after {attempts} attempts")


def _declare_queue_with_dlq(channel: BlockingChannel, name: str) -> None:
    dlq = f"{name}.dlq"
    channel.queue_declare(queue=dlq, durable=True)
    channel.queue_bind(queue=dlq, exchange=DEAD_LETTER_EXCHANGE, routing_key=name)
    channel.queue_declare(
        queue=name,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
            "x-dead-letter-routing-key": name,
        },
    )


def declare_topology(channel: BlockingChannel) -> None:
    """Idempotent: every service declares the same queues on startup."""
    channel.exchange_declare(
        exchange=DEAD_LETTER_EXCHANGE, exchange_type="direct", durable=True
    )
    _declare_queue_with_dlq(channel, CHECK_JOBS_QUEUE)
    _declare_queue_with_dlq(channel, STATUS_CHANGES_QUEUE)


def publish(channel: BlockingChannel, queue: str, payload: dict) -> None:
    """Publish a persistent JSON message so it survives a broker restart."""
    channel.basic_publish(
        exchange="",
        routing_key=queue,
        body=json.dumps(payload).encode(),
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,  # persistent
        ),
    )
