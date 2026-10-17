"""HTTP API: manage monitors, read results, and a simple status page."""

import html
import logging
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from uptime.config import configure_logging
from uptime.db import SessionLocal
from uptime.models import CheckResult, Monitor

configure_logging("api")
log = logging.getLogger(__name__)

app = FastAPI(title="Uptime Platform API", version="0.1.0")


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


# ---------- Schemas ----------

class MonitorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: HttpUrl
    expected_status: int = Field(default=200, ge=100, le=599)


class MonitorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    expected_status: int
    is_active: bool
    current_status: str
    last_checked_at: datetime | None


class CheckResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    checked_at: datetime
    is_up: bool
    status_code: int | None
    response_time_ms: int | None
    error: str | None


# ---------- Health endpoints ----------

@app.get("/healthz", tags=["health"])
def liveness() -> dict:
    """Liveness: the process is alive. No dependency checks on purpose, so a
    database outage doesn't make Kubernetes restart every API pod."""
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
def readiness(session: Session = Depends(get_session)) -> dict:
    """Readiness: can this instance serve requests right now?"""
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        log.warning("Readiness check failed: database unreachable")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unreachable")
    return {"status": "ready"}


# ---------- Monitors ----------

@app.post("/monitors", response_model=MonitorOut, status_code=status.HTTP_201_CREATED)
def create_monitor(payload: MonitorCreate, session: Session = Depends(get_session)) -> Monitor:
    monitor = Monitor(
        name=payload.name,
        url=str(payload.url),
        expected_status=payload.expected_status,
    )
    session.add(monitor)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "a monitor for this URL already exists")
    log.info("Created monitor %s for %s", monitor.id, monitor.url)
    return monitor


@app.get("/monitors", response_model=list[MonitorOut])
def list_monitors(session: Session = Depends(get_session)) -> list[Monitor]:
    return list(session.scalars(select(Monitor).order_by(Monitor.id)))


@app.get("/monitors/{monitor_id}", response_model=MonitorOut)
def get_monitor(monitor_id: int, session: Session = Depends(get_session)) -> Monitor:
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "monitor not found")
    return monitor


@app.delete("/monitors/{monitor_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_monitor(monitor_id: int, session: Session = Depends(get_session)) -> Response:
    monitor = session.get(Monitor, monitor_id)
    if monitor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "monitor not found")
    session.delete(monitor)
    session.commit()
    log.info("Deleted monitor %s", monitor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/monitors/{monitor_id}/results", response_model=list[CheckResultOut])
def monitor_results(
    monitor_id: int,
    limit: int = Query(default=20, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[CheckResult]:
    if session.get(Monitor, monitor_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "monitor not found")
    query = (
        select(CheckResult)
        .where(CheckResult.monitor_id == monitor_id)
        .order_by(CheckResult.checked_at.desc())
        .limit(limit)
    )
    return list(session.scalars(query))


# ---------- Status page ----------

_STATUS_COLOURS = {"up": "#1a7f37", "down": "#cf222e", "unknown": "#6e7781"}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def status_page(session: Session = Depends(get_session)) -> str:
    monitors = session.scalars(select(Monitor).order_by(Monitor.name))
    rows = []
    for m in monitors:
        colour = _STATUS_COLOURS.get(m.current_status, "#6e7781")
        checked = (
            m.last_checked_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if m.last_checked_at
            else "never"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(m.name)}</td>"
            f"<td>{html.escape(m.url)}</td>"
            f'<td style="color:{colour};font-weight:600">{html.escape(m.current_status)}</td>'
            f"<td>{checked}</td>"
            "</tr>"
        )
    body = "".join(rows) or '<tr><td colspan="4">No monitors yet. Add one with POST /monitors.</td></tr>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>Status</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem}}
 table{{width:100%;border-collapse:collapse}}
 th,td{{text-align:left;padding:.5rem;border-bottom:1px solid #ddd}}
</style></head>
<body><h1>Service status</h1>
<table><thead><tr><th>Name</th><th>URL</th><th>Status</th><th>Last checked</th></tr></thead>
<tbody>{body}</tbody></table>
<p style="color:#6e7781">Refreshes every 30 seconds. API docs at <a href="/docs">/docs</a>.</p>
</body></html>"""
