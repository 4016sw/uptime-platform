"""Worker: consume check jobs, check URLs, store results, emit status changes.

Scaling: each worker processes one job at a time (prefetch=1). To check more
URLs per minute, run more worker replicas. In Phase 1, KEDA scales the
replicas automatically based on the length of the check_jobs queue.
"""

import json
import logging
import signal
from datetime import datetime, timezone

from uptime.config import configure_logging, get_settings
from uptime.db import SessionLocal
from uptime.messaging import (
    CHECK_JOBS_QUEUE,
    STATUS_CHANGES_QUEUE,
    connect,
    declare_topology,
    publish,
)
from uptime.models import CheckResult, Monitor
from uptime.worker.checker import check_url

log = logging.getLogger(__name__)


def is_alertable_change(previous: str, current: str) -> bool:
    """Alert when status flips. The very first check alerts only if it's down."""
    if previous == current:
        return False
    if previous == "unknown":
        return current == "down"
    return True


def process(monitor_id: int, timeout_seconds: float) -> dict | None:
    """Run one check. Returns a status-change event, or None if nothing changed."""
    # Read what we need, then release the DB connection BEFORE the HTTP call.
    # Holding a connection during a slow (up to 10s) request would tie up the
    # pool and could exhaust PostgreSQL's max_connections under load.
    with SessionLocal() as session:
        monitor = session.get(Monitor, monitor_id)
        if monitor is None or not monitor.is_active:
            log.info("Monitor %s deleted or inactive, skipping", monitor_id)
            return None
        url, expected_status, name = monitor.url, monitor.expected_status, monitor.name

    outcome = check_url(url, expected_status, timeout_seconds)
    now = datetime.now(timezone.utc)
    current = "up" if outcome.is_up else "down"

    with SessionLocal.begin() as session:
        # Row lock so two workers checking the same monitor can't race on
        # current_status and both (or neither) fire an alert.
        monitor = session.get(Monitor, monitor_id, with_for_update=True)
        if monitor is None:
            return None
        previous = monitor.current_status
        session.add(
            CheckResult(
                monitor_id=monitor_id,
                checked_at=now,
                is_up=outcome.is_up,
                status_code=outcome.status_code,
                response_time_ms=outcome.response_time_ms,
                error=outcome.error,
            )
        )
        monitor.current_status = current
        monitor.last_checked_at = now

    log.info(
        "Checked monitor %s (%s): %s status=%s time=%sms",
        monitor_id, url, current, outcome.status_code, outcome.response_time_ms,
    )

    if not is_alertable_change(previous, current):
        return None
    return {
        "monitor_id": monitor_id,
        "name": name,
        "url": url,
        "previous_status": previous,
        "current_status": current,
        "status_code": outcome.status_code,
        "error": outcome.error,
        "checked_at": now.isoformat(),
    }


def main() -> None:
    configure_logging("worker")
    settings = get_settings()

    connection = connect(settings.rabbitmq_url)
    channel = connection.channel()
    declare_topology(channel)
    channel.basic_qos(prefetch_count=settings.worker_prefetch)

    def on_message(ch, method, _properties, body: bytes) -> None:
        try:
            monitor_id = int(json.loads(body)["monitor_id"])
        except (ValueError, KeyError, TypeError):
            log.error("Malformed job, sending to DLQ: %r", body)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        try:
            event = process(monitor_id, settings.check_timeout_seconds)
            if event:
                publish(ch, STATUS_CHANGES_QUEUE, event)
                log.warning("Status change for monitor %s: %s -> %s",
                            monitor_id, event["previous_status"], event["current_status"])
            # Ack only after the result is saved: if the worker crashes
            # mid-job, RabbitMQ redelivers the message to another worker.
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception:
            log.exception("Failed to process monitor %s, sending to DLQ", monitor_id)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    # Graceful shutdown: on SIGTERM (Kubernetes pod termination) finish the
    # current message, then stop consuming.
    def _stop(signum, _frame) -> None:
        log.info("Received signal %s, stopping", signum)
        channel.stop_consuming()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    channel.basic_consume(queue=CHECK_JOBS_QUEUE, on_message_callback=on_message)
    log.info("Worker started, waiting for jobs")
    try:
        channel.start_consuming()
    finally:
        if connection.is_open:
            connection.close()


if __name__ == "__main__":
    main()
