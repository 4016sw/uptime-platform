"""Block until TCP dependencies accept connections.

Used as a Kubernetes initContainer:
    python -m uptime.wait_for postgres:5432 rabbitmq:5672

Kubernetes has no depends_on, so a pod waits for its dependencies here
instead of crash-looping until they're up. The app still has its own
retries, because a dependency can also go away after startup.
"""

import socket
import sys
import time


def wait_for(host: str, port: int, timeout_seconds: float = 300, interval_seconds: float = 2) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"{host}:{port} is accepting connections", flush=True)
                return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise SystemExit(f"Gave up waiting for {host}:{port}: {exc!r}") from exc
            print(f"Waiting for {host}:{port} ({exc.__class__.__name__})", flush=True)
            time.sleep(interval_seconds)


def main(targets: list[str]) -> None:
    if not targets:
        raise SystemExit("usage: python -m uptime.wait_for host:port [host:port ...]")
    for target in targets:
        host, _, port = target.rpartition(":")
        wait_for(host, int(port))


if __name__ == "__main__":
    main(sys.argv[1:])
