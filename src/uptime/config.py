"""Configuration loaded from environment variables.

Every setting comes from the environment so the same image runs unchanged
in Docker Compose, Kubernetes (ConfigMaps/Secrets) and CI.
"""

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote

from sqlalchemy.engine import URL


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    """Use DATABASE_URL if set, otherwise build it from separate parts.

    Separate parts let Kubernetes keep the password in a Secret and the
    rest in a ConfigMap. URL.create also escapes special characters in
    the password, which string formatting would not.
    """
    if url := os.getenv("DATABASE_URL"):
        return url
    return URL.create(
        drivername="postgresql+psycopg",
        username=os.getenv("DB_USER", "uptime"),
        password=os.getenv("DB_PASSWORD", ""),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "uptime"),
    ).render_as_string(hide_password=False)


def _rabbitmq_url() -> str:
    """Use RABBITMQ_URL if set, otherwise build it from separate parts.

    Same reason as the database URL: in Kubernetes the password comes from
    a Secret and the host/user from a ConfigMap.
    """
    if url := os.getenv("RABBITMQ_URL"):
        return url
    user = quote(os.getenv("RABBITMQ_USER", "guest"), safe="")
    password = quote(os.getenv("RABBITMQ_PASSWORD", "guest"), safe="")
    host = os.getenv("RABBITMQ_HOST", "localhost")
    port = os.getenv("RABBITMQ_PORT", "5672")
    return f"amqp://{user}:{password}@{host}:{port}/"


@dataclass(frozen=True)
class Settings:
    database_url: str
    rabbitmq_url: str
    check_timeout_seconds: float
    schedule_interval_seconds: int
    scheduler_loop: bool
    worker_prefetch: int
    telegram_bot_token: str
    telegram_chat_id: str
    log_level: str


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=_database_url(),
        rabbitmq_url=_rabbitmq_url(),
        check_timeout_seconds=float(os.getenv("CHECK_TIMEOUT_SECONDS", "10")),
        schedule_interval_seconds=int(os.getenv("SCHEDULE_INTERVAL_SECONDS", "60")),
        scheduler_loop=_env_bool("SCHEDULER_LOOP", False),
        worker_prefetch=int(os.getenv("WORKER_PREFETCH", "1")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


def configure_logging(service: str) -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format=f"%(asctime)s %(levelname)s [{service}] %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO. The Telegram URL contains the bot
    # token, so keep these logs at WARNING to avoid leaking it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("pika").setLevel(logging.WARNING)
