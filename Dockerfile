# One image for all four roles (api, scheduler, worker, alerter).
# Each container picks its role through the command it runs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is reused unless requirements.txt changes,
# so code-only changes rebuild in seconds.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY src ./src

# Non-root user with a fixed numeric UID. Kubernetes can then verify it with
# securityContext.runAsNonRoot (it can't verify a user given only by name).
RUN useradd --create-home --uid 10001 appuser
USER 10001

EXPOSE 8000

# Default role: the API. Compose/Kubernetes override this for the others.
CMD ["uvicorn", "uptime.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
