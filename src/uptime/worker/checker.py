"""The actual URL check. Pure logic with no database or queue, so it's easy to unit-test."""

import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class CheckOutcome:
    is_up: bool
    status_code: int | None
    response_time_ms: int | None
    error: str | None


def check_url(
    url: str,
    expected_status: int,
    timeout_seconds: float,
    client: httpx.Client | None = None,
) -> CheckOutcome:
    """Request the URL and decide whether it's up.

    "Up" means the response status equals expected_status. Timeouts and
    connection errors count as down, with the reason recorded.
    """
    owns_client = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True)

    start = time.perf_counter()
    try:
        response = client.get(url, timeout=timeout_seconds)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        is_up = response.status_code == expected_status
        error = None if is_up else f"Expected HTTP {expected_status}, got {response.status_code}"
        return CheckOutcome(is_up, response.status_code, elapsed_ms, error)
    except httpx.TimeoutException:
        return CheckOutcome(False, None, None, f"Timed out after {timeout_seconds:g}s")
    except httpx.HTTPError as exc:
        return CheckOutcome(False, None, None, f"{type(exc).__name__}: {exc}")
    finally:
        if owns_client:
            client.close()
