# Uptime Platform

[![CI](https://github.com/4016sw/uptime-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/4016sw/uptime-platform/actions/workflows/ci.yml)

A self-hosted uptime monitoring and alerting platform. Register URLs, the platform checks them every minute, stores the results, and sends a Telegram alert when a site goes down or recovers.

The application is deliberately small. The point of this project is the **platform around it**: containers, Kubernetes, CI/CD, GitOps, observability, infrastructure as code and reliability practices, built up in phases.

## Architecture

```
                 ┌──────────────┐
  User ────────► │  API         │ ◄──── status page (/)
                 │  (FastAPI)   │
                 └──────┬───────┘
                        │
                        ▼
                 ┌──────────────┐
                 │  PostgreSQL  │ ◄──────────────┐
                 └──────┬───────┘                │ save results
                        │ active monitors        │
                        ▼                        │
                 ┌──────────────┐  check_jobs  ┌─┴──────────┐
                 │  Scheduler   │ ───────────► │  Workers   │ ──► HTTP check
                 └──────────────┘   RabbitMQ   └─────┬──────┘
                                                     │ status_changes
                                                     ▼  (RabbitMQ)
                                              ┌────────────┐
                                              │  Alerter   │ ──► Telegram
                                              └────────────┘
```

| Component | Responsibility |
|---|---|
| **API** | Create, list and delete monitors; read check history; status page; `/healthz` and `/readyz` |
| **Scheduler** | Publishes one job per active monitor to `check_jobs`. One-shot by default (a Kubernetes CronJob in Phase 1); loops every 60s in Compose |
| **Worker** | Consumes jobs, checks the URL, saves the result, publishes an event to `status_changes` when status flips |
| **Alerter** | Sends a Telegram message for each status change (logs only if Telegram isn't configured) |
| **PostgreSQL** | `monitors` and `check_results` tables, managed with Alembic migrations |
| **RabbitMQ** | Durable queues with dead-letter queues (`*.dlq`) for messages that can't be processed |

All four roles are built from **one Docker image** and differ only in the command they run.

## Run it locally

Requirements: Docker with Docker Compose v2.

```bash
cp .env.example .env          # then edit the passwords
docker compose up --build -d
docker compose ps             # all services should be running/healthy; migrate shows "exited (0)"
```

| URL | What |
|---|---|
| http://localhost:8000 | Status page |
| http://localhost:8000/docs | Interactive API docs |
| http://localhost:15672 | RabbitMQ management UI (user/password from `.env`) |

## Try it

```bash
# A healthy site
curl -X POST localhost:8000/monitors -H 'Content-Type: application/json' \
  -d '{"name": "Example", "url": "https://example.com"}'

# A "broken" site: httpbin returns HTTP 500, so this monitor goes DOWN and fires an alert
curl -X POST localhost:8000/monitors -H 'Content-Type: application/json' \
  -d '{"name": "Broken demo", "url": "https://httpbin.org/status/500"}'

curl localhost:8000/monitors
curl localhost:8000/monitors/2/results

docker compose logs -f worker alerter
```

Within about a minute, the alerter logs (or sends to Telegram) a `DOWN` alert for the broken monitor.

**Scale the workers:**

```bash
docker compose up -d --scale worker=3
```

### Telegram alerts (optional)

1. Message **@BotFather** on Telegram, create a bot, copy the token.
2. Send any message to your new bot, then get your chat ID (e.g. from **@userinfobot**).
3. Put both in `.env` and run `docker compose up -d alerter`.

## Run it on Kubernetes

See **[deploy/k8s/README.md](deploy/k8s/README.md)** for running the platform on minikube with StatefulSets, a CronJob scheduler, probes, Ingress and optional KEDA autoscaling.

## CI/CD

Every push and pull request runs the GitHub Actions pipeline in [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

```
pull request:  lint + tests → build image → vulnerability scan
push to main:  lint + tests → build image → vulnerability scan → push to GHCR
```

| Step | Tool | Fails the pipeline when |
|---|---|---|
| Lint | ruff | Code has errors (unused imports, undefined names, syntax problems) |
| Unit tests | pytest | Any test fails |
| Build | Docker Buildx, with layer caching | The image doesn't build |
| Vulnerability scan | Trivy | The image has a **CRITICAL** vulnerability with a fix available (HIGH ones are reported) |
| Publish (main only) | GitHub Container Registry | — |

Published images are tagged with the **short commit SHA**, e.g. `ghcr.io/4016sw/uptime-platform:a1b2c3d`, plus `main` for the latest build of the main branch. Deployments use the SHA tag, so every running version maps to an exact commit and rolling back means deploying an older SHA.

**Design choices**

- **Tests gate the build:** the image job only runs if lint and tests pass.
- **Scan before publish:** the image is built locally in the runner, scanned, and only then pushed, so a vulnerable image never reaches the registry.
- **No stored credentials:** pushing uses the short-lived `GITHUB_TOKEN` that GitHub creates for each run, with `packages: write` granted only to the image job.
- **Pull requests never publish:** they get the full checks without the push step.
- **Superseded runs are cancelled:** a new push to the same branch cancels the older run.
- **Dependabot** opens weekly pull requests for Python packages, GitHub Actions and the base image; each one goes through the pipeline before merging.

## Tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Design decisions

- **Queue between scheduler and workers.** Checks are independent jobs, so workers scale horizontally and a crashed worker doesn't lose work: messages are acknowledged only after the result is saved, so unacknowledged jobs are redelivered.
- **Dead-letter queues.** A message that fails processing is rejected into `<queue>.dlq` instead of being retried forever (a poison message) or silently dropped.
- **No DB connection held during HTTP checks.** The worker reads the monitor, releases the connection, does the (up to 10s) HTTP request, then writes the result. Holding connections through slow network calls is a classic way to exhaust PostgreSQL's `max_connections`.
- **Row lock on status updates.** `SELECT ... FOR UPDATE` prevents two workers checking the same monitor from racing and firing duplicate (or missing) alerts.
- **Alert on state change only.** A site that stays down alerts once, not every minute. The first check alerts only if the site is down.
- **Constraints in the database.** `monitors.url` is unique at the database level, not just checked in code.
- **Liveness vs readiness.** `/healthz` doesn't touch the database, so a database outage doesn't make Kubernetes restart every API pod. `/readyz` does, so traffic stops going to an instance that can't serve it.
- **Non-root container** with a fixed numeric UID (10001), so Kubernetes can enforce `runAsNonRoot`.
- **Configuration only through environment variables**, so the same image runs in Compose, Kubernetes and CI. The database password can come from its own variable, ready to live in a Kubernetes Secret.
- **Secrets stay out of logs.** HTTP client request logging is turned down because the Telegram API URL contains the bot token.

## Known limitations (planned for later phases)

- If PostgreSQL is down, jobs fail and land in the DLQ. Checks self-heal (the scheduler re-publishes every minute), but a retry-with-delay queue would be cleaner.
- If publishing a status-change event fails after the result is saved, that one alert is missed (no transactional outbox yet).
- `check_results` grows forever: needs a retention job.
- No authentication on the API.
- No metrics yet: Prometheus metrics arrive in Phase 4.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Application + Docker Compose | ✅ |
| 1 | Kubernetes: Deployments, StatefulSets for PostgreSQL/RabbitMQ, CronJob, probes, resource limits, Ingress, KEDA autoscaling ([guide](deploy/k8s/README.md)) | ✅ |
| 2 | CI/CD with GitHub Actions: tests, image build, Trivy scan, SHA-tagged images on GHCR, Dependabot | ✅ |
| 3 | Helm chart + GitOps with Argo CD | ⏳ |
| 4 | Observability: Prometheus metrics, Grafana dashboards, Alertmanager, SLOs | ⏳ |
| 5 | AWS with Terraform: modules, S3 remote state, EKS, RDS, GitHub OIDC | ⏳ |
| 6 | Reliability: backups and restore tests, failure drills, runbooks, postmortems | ⏳ |
