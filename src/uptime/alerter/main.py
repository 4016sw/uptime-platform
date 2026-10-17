"""Alerter: turn status-change events into Telegram messages.

If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing, alerts are only logged
("dry run"), so the platform still works locally without Telegram.
"""

import json
import logging
import signal

import httpx

from uptime.config import configure_logging, get_settings
from uptime.messaging import STATUS_CHANGES_QUEUE, connect, declare_topology

log = logging.getLogger(__name__)


def format_message(event: dict) -> str:
    name = event.get("name", "unknown")
    url = event.get("url", "")
    if event.get("current_status") == "down":
        reason = event.get("error") or "no details"
        return f"🔴 DOWN: {name}\n{url}\nReason: {reason}\nAt: {event.get('checked_at')}"
    return f"🟢 RECOVERED: {name}\n{url}\nAt: {event.get('checked_at')}"


def send_telegram(token: str, chat_id: str, text: str, client: httpx.Client | None = None) -> None:
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=10)
    try:
        response = client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        response.raise_for_status()
    finally:
        if owns_client:
            client.close()


def main() -> None:
    configure_logging("alerter")
    settings = get_settings()
    telegram_enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)
    if not telegram_enabled:
        log.warning("Telegram not configured: alerts will only be logged (dry run)")

    connection = connect(settings.rabbitmq_url)
    channel = connection.channel()
    declare_topology(channel)
    channel.basic_qos(prefetch_count=1)

    def on_message(ch, method, _properties, body: bytes) -> None:
        try:
            event = json.loads(body)
        except ValueError:
            log.error("Malformed event, sending to DLQ: %r", body)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        text = format_message(event)
        try:
            if telegram_enabled:
                send_telegram(settings.telegram_bot_token, settings.telegram_chat_id, text)
                log.info("Alert sent for monitor %s", event.get("monitor_id"))
            else:
                log.warning("ALERT (dry run): %s", text.replace("\n", " | "))
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except httpx.HTTPError:
            # Don't requeue forever if Telegram is down: park it in the DLQ.
            log.exception("Telegram send failed for monitor %s, sending to DLQ", event.get("monitor_id"))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def _stop(signum, _frame) -> None:
        log.info("Received signal %s, stopping", signum)
        channel.stop_consuming()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    channel.basic_consume(queue=STATUS_CHANGES_QUEUE, on_message_callback=on_message)
    log.info("Alerter started, waiting for status changes")
    try:
        channel.start_consuming()
    finally:
        if connection.is_open:
            connection.close()


if __name__ == "__main__":
    main()
