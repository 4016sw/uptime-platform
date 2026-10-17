import pytest

from uptime.alerter.main import format_message
from uptime.worker.main import is_alertable_change


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("unknown", "up", False),   # first check, healthy: no noise
        ("unknown", "down", True),  # first check, broken: alert
        ("up", "up", False),
        ("down", "down", False),    # still down: don't alert every minute
        ("up", "down", True),
        ("down", "up", True),       # recovery
    ],
)
def test_is_alertable_change(previous, current, expected):
    assert is_alertable_change(previous, current) is expected


def test_down_message_includes_reason():
    text = format_message(
        {"name": "Shop", "url": "https://shop.example", "current_status": "down",
         "error": "Expected HTTP 200, got 503", "checked_at": "2026-10-17T10:00:00+00:00"}
    )
    assert text.startswith("🔴 DOWN: Shop")
    assert "503" in text


def test_recovery_message():
    text = format_message({"name": "Shop", "url": "https://shop.example", "current_status": "up"})
    assert text.startswith("🟢 RECOVERED: Shop")
