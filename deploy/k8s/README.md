# Phase 1: Kubernetes

The whole platform running on a local Kubernetes cluster (minikube), managed with plain manifests and Kustomize.

## What runs where

| Component | Kubernetes object | Why this object |
|---|---|---|
| PostgreSQL | **StatefulSet** + PVC + headless Service | Needs persistent storage and a stable identity |
| RabbitMQ | **StatefulSet** + PVC + Services | Persistent queues; its node name is tied to a stable hostname |
| Migrations | **Job** | Runs `alembic upgrade head` once, then completes |
| API | **Deployment** (2 replicas) + Service + Ingress + PodDisruptionBudget | Stateless, horizontally scalable, reachable from outside |
| Worker | **Deployment** (scaled by KEDA, optional) | Stateless consumers; scale with queue length |
| Alerter | **Deployment** (1 replica) | Stateless consumer |
| Scheduler | **CronJob** (every minute) | Short task on a schedule |
| Settings | **ConfigMap** (generated) | Non-secret config |
| Passwords, Telegram token | **Secret** (generated from a gitignored file) | Kept out of Git |

All objects live in the `uptime` namespace.

## Prerequisites

- minikube, kubectl
- Optional: Helm (only for the KEDA addon)

## 1. Start minikube

```bash
minikube start --cpus 2 --memory 4096
minikube addons enable ingress
```

## 2. Build the image inside minikube

The cluster has its own image store, so build the image there:

```bash
minikube image build -t uptime-platform:dev .
minikube image ls | grep uptime-platform
```

## 3. Create the secrets file

```bash
cp deploy/k8s/secrets.env.example deploy/k8s/secrets.env
nano deploy/k8s/secrets.env      # set the two passwords (Telegram optional)
```

`secrets.env` is gitignored. Kustomize turns it into a Kubernetes Secret.

## 4. Deploy

```bash
kubectl apply -k deploy/k8s
kubectl get pods -n uptime -w     # Ctrl+C once everything is Running/Completed
```

Startup order is handled automatically: PostgreSQL and RabbitMQ start, app pods wait in `Init` until their dependencies accept connections, the migration Job creates the tables, and then the apps start.

```bash
kubectl wait --for=condition=complete job/uptime-migrate -n uptime --timeout=180s
kubectl get all,pvc -n uptime
```

## 5. Use it

**Option A: port-forward (simplest)**

```bash
kubectl port-forward -n uptime svc/api 8000:80
# then, in another terminal:
curl localhost:8000/healthz
```

**Option B: through the Ingress**

```bash
echo "$(minikube ip) uptime.local" | sudo tee -a /etc/hosts
curl http://uptime.local/healthz
```

Add monitors (use `localhost:8000` or `uptime.local`):

```bash
curl -X POST localhost:8000/monitors -H 'Content-Type: application/json' \
  -d '{"name": "Example", "url": "https://example.com"}'
curl -X POST localhost:8000/monitors -H 'Content-Type: application/json' \
  -d '{"name": "Broken demo", "url": "https://httpbin.org/status/500"}'
```

Watch it work:

```bash
kubectl get cronjob,jobs -n uptime          # a new scheduler Job every minute
kubectl logs -n uptime deploy/uptime-worker -f
kubectl logs -n uptime deploy/uptime-alerter -f
```

RabbitMQ UI:

```bash
kubectl port-forward -n uptime svc/rabbitmq 15672:15672    # http://localhost:15672
```

## 6. Experiments (good interview stories)

**Data survives a pod restart (StatefulSet + PVC):**

```bash
kubectl delete pod postgres-0 -n uptime
kubectl get pods -n uptime -w              # postgres-0 comes back with the same name
curl localhost:8000/monitors               # monitors are still there (restart the port-forward if it dropped)
```

**Readiness in action:** while `postgres-0` restarts, run `kubectl get pods -n uptime`. The API pods show `0/1 READY` (their `/readyz` fails), so the Service stops sending them traffic, but they are **not restarted**, because `/healthz` (liveness) doesn't depend on the database.

**Self-healing:** delete a worker pod and watch the Deployment replace it. Any job it was processing is redelivered, because it was never acknowledged.

```bash
kubectl delete pod -n uptime -l app.kubernetes.io/name=worker
```

**Zero-downtime rollout:**

```bash
kubectl rollout restart deployment/uptime-api -n uptime
kubectl rollout status deployment/uptime-api -n uptime
```

With `maxUnavailable: 0`, a new pod must become Ready before an old one is removed.

**Config change rolls out automatically:** change `LOG_LEVEL=INFO` to `DEBUG` in `kustomization.yaml` and re-apply. The ConfigMap gets a new hashed name, so every Deployment that uses it rolls out new pods.

## 7. Autoscaling workers with KEDA (optional)

Install KEDA:

```bash
helm repo add kedacore https://kedacore.github.io/charts
helm repo update
helm install keda kedacore/keda --namespace keda --create-namespace
```

Configure and apply the addon:

```bash
cp deploy/k8s/addons/keda/keda.env.example deploy/k8s/addons/keda/keda.env
nano deploy/k8s/addons/keda/keda.env       # same RabbitMQ password as secrets.env
kubectl apply -k deploy/k8s/addons/keda
kubectl get scaledobject,hpa -n uptime
```

Create load: 60 monitors pointing at a slow endpoint (each check takes ~5s), so jobs pile up faster than one worker can handle them:

```bash
for i in $(seq 1 60); do
  curl -s -o /dev/null -X POST localhost:8000/monitors -H 'Content-Type: application/json' \
    -d "{\"name\": \"slow-$i\", \"url\": \"https://httpbin.org/delay/5?n=$i\"}"
done
kubectl get hpa,pods -n uptime -w
```

Within a minute or two KEDA scales the workers up; after the load is gone (delete the slow monitors) it scales back down after the cooldown.

## 8. Updating the code

```bash
minikube image build -t uptime-platform:dev .
kubectl rollout restart deployment -n uptime
```

The restart is needed because the tag (`dev`) didn't change, so Kubernetes has no reason to pull or restart anything. Phase 2 fixes this properly with a unique image tag per commit.

## 9. Clean up

```bash
kubectl delete -k deploy/k8s
kubectl delete pvc --all -n uptime     # StatefulSet volumes are NOT deleted automatically
```

PVCs created by a StatefulSet are kept on purpose when the StatefulSet is deleted, so data isn't lost by accident.

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `ErrImagePull` / `ImagePullBackOff` on app pods | Image not built inside minikube | `minikube image ls \| grep uptime` |
| Pod stuck in `Init:0/1` | Waiting for PostgreSQL/RabbitMQ | `kubectl logs <pod> -c wait-for-dependencies -n uptime` |
| `uptime-migrate` failed | DB not reachable or wrong password | `kubectl logs job/uptime-migrate -n uptime` |
| `field is immutable` when applying | Job changed while the old one still exists | `kubectl delete job uptime-migrate -n uptime`, then apply again |
| `secrets.env: no such file` | Secrets file not created | Step 3 |
| PVC stuck in `Pending` | No storage provisioner | `minikube addons enable storage-provisioner` |
| Pod `Running` but `0/1 READY` | Readiness probe failing | `kubectl describe pod <pod> -n uptime` (Events) |
| RabbitMQ login fails after changing the password | The default user is only created on first boot | Delete the RabbitMQ PVC to start fresh |

## Design decisions

- **StatefulSet vs Deployment.** PostgreSQL and RabbitMQ get stable pod names (`postgres-0`) and their own persistent volume that follows the pod. Deployments are for interchangeable, stateless pods.
- **Headless Service for StatefulSets.** Gives each pod a stable DNS name; `postgres` resolves straight to the pod.
- **initContainers for startup order.** Kubernetes has no `depends_on`. Pods wait until their dependencies accept TCP connections instead of crash-looping. The apps still retry on their own, since dependencies can also disappear later.
- **Readiness vs liveness.** API readiness checks the database, so traffic stops when it can't serve; liveness doesn't, so a database outage doesn't trigger a restart storm. RabbitMQ readiness checks that it accepts client connections, not just that the process is up.
- **Migrations as a Job.** Runs once, separately from the app, so several API replicas never race to migrate. Jobs are immutable, so it cleans itself up with `ttlSecondsAfterFinished`.
- **CronJob settings.** `concurrencyPolicy: Forbid` and `backoffLimit: 0` avoid duplicate scheduling; a missed run is covered by the next one a minute later.
- **Requests and memory limits, no CPU limits.** Requests let the scheduler place pods sensibly; memory limits cap runaway processes (OOMKilled); CPU limits are left off to avoid throttling while the node has spare CPU.
- **Hardened containers.** Non-root UID 10001, no privilege escalation, read-only root filesystem, all Linux capabilities dropped, default seccomp profile.
- **Least privilege for secrets.** Each component gets only the secrets it uses: the alerter gets the Telegram token but not the database password.
- **Generated ConfigMap/Secret names.** A content hash in the name means any config change automatically rolls out new pods.
- **No `replicas` on the worker.** The autoscaler owns the replica count; a fixed value in the manifest would be reset on every apply.
- **PodDisruptionBudget for the API.** Node drains and upgrades keep at least one API pod running.

## Known limitations (later phases)

- Kubernetes Secrets are only base64-encoded, not encrypted. Production would use encryption at rest plus Sealed Secrets or External Secrets Operator with a secrets manager.
- Single-instance PostgreSQL and RabbitMQ: no replication or failover yet.
- Worker and alerter have no liveness probe until Phase 4 adds a metrics endpoint.
- No NetworkPolicies yet (the default minikube network plugin doesn't enforce them).
- The `dev` image tag requires manual restarts; Phase 2 introduces per-commit tags.
