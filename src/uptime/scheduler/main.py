"""Scheduler: publish one check job per active monitor.

Two modes:
  - one-shot (default): run once and exit. In Kubernetes this runs as a
    CronJob every minute, so Kubernetes owns the schedule.
  - loop (SCHEDULER_LOOP=true): run every SCHEDULE_INTERVAL_SECONDS. Used in
    Docker Compose, which has no built-in cron.
"""

import logging
import signal
from datetime import UTC, datetime

import pika
from sqlalchemy import select

from uptime.config import configure_logging, get_settings
from uptime.db import SessionLocal
from uptime.messaging import CHECK_JOBS_QUEUE, connect, declare_topology, publish
from uptime.models import Monitor

log = logging.getLogger(__name__)
_stopping = False


def _request_stop(signum, _frame) -> None:
    global _stopping
    log.info("Received signal %s, stopping after the current cycle", signum)
    _stopping = True


def run_once(channel) -> int:
    with SessionLocal() as session:
        monitor_ids = session.scalars(
            select(Monitor.id).where(Monitor.is_active.is_(True))
        ).all()

    scheduled_at = datetime.now(UTC).isoformat()
    for monitor_id in monitor_ids:
        publish(channel, CHECK_JOBS_QUEUE, {"monitor_id": monitor_id, "scheduled_at": scheduled_at})

    log.info("Scheduled %d check(s)", len(monitor_ids))
    return len(monitor_ids)


def main() -> None:
    configure_logging("scheduler")
    settings = get_settings()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    connection = connect(settings.rabbitmq_url)
    channel = connection.channel()
    declare_topology(channel)
    # Publisher confirms: the broker acknowledges each message, so a failed
    # publish raises an error instead of silently losing the job.
    channel.confirm_delivery()

    try:
        if not settings.scheduler_loop:
            run_once(channel)
            return
        while not _stopping:
            try:
                run_once(channel)
            except pika.exceptions.AMQPError:
                log.exception("Publishing failed; will retry next cycle")
            # connection.sleep keeps RabbitMQ heartbeats flowing while we wait.
            for _ in range(settings.schedule_interval_seconds):
                if _stopping:
                    break
                connection.sleep(1)
    finally:
        if connection.is_open:
            connection.close()


if __name__ == "__main__":
    main()
