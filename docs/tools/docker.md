# Docker: Hardened Container Images

> **CzarIntel Landing Zone** | Tool guide | Sources: [`docker/api-service/`](../../docker/api-service), [`docker/frontend/`](../../docker/frontend)

Both images follow one standard: **multi-stage, pinned base, non-root, no shell tooling in runtime, healthcheck, OCI labels**. `compliance_scan.py` enforces the standard on every PR (`DOCKER-001` non-root USER, `DOCKER-002` pinned base, `DOCKER-003` HEALTHCHECK), and `hadolint --failure-threshold warning` runs in Jenkins.

## api-service

A FastAPI reference service. It is the workload the whole platform (CI, GitOps, SLOs and dashboards) is demonstrated against.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness: status, version, environment, uptime |
| `GET /ready` | Readiness. If `DB_HOST` is set, TCP-checks MySQL and returns **503** when unreachable, so traffic is withdrawn before errors surface |
| `GET /api/v1/services` / `/api/v1/services/{name}` | Platform service catalogue (per-cloud status) |
| `GET /metrics` | Prometheus exposition (private registry) |

**Metrics** feed the SLO rules and the `api-slo` dashboard:

- `http_requests_total{method,path,status}`
- `http_request_duration_seconds` (histogram)
- `http_requests_in_flight`
- `czarintel_service_up{service,cloud}`
- `czarintel_dependency_up{dependency}`
- `czarintel_build_info`

`path` uses the **route template** (e.g. `/api/v1/services/{name}`) so label cardinality stays bounded.

**Configuration** (env): `APP_ENV`, `APP_VERSION`, `LOG_LEVEL`, `PORT`, `DB_HOST`, `DB_PORT`, `DB_CHECK_TIMEOUT`. Logs are single-line JSON, which Fluent Bit and Loki parse without regex.

### Image anatomy

| Stage | Base | Contents |
|---|---|---|
| `builder` | `python:3.12-slim-bookworm` | `build-essential`, a virtualenv at `/opt/venv`, wheels from pinned `requirements.txt` |
| `runtime` | `python:3.12-slim-bookworm` | `apt-get upgrade` for OS patches, then apt lists removed; only `/opt/venv` and `app.py` copied |

- User `app` **UID/GID 10001**, shell `/usr/sbin/nologin`, `USER 10001:10001`. This matches the Helm `runAsUser` and Gatekeeper expectations.
- `HEALTHCHECK` uses Python's `urllib`, so neither curl nor wget is needed in the image.
- `ENTRYPOINT uvicorn … --workers 2 --proxy-headers --no-server-header`: no version banner leak.
- Labels: `org.opencontainers.image.{title,version,revision,created,vendor,licenses}`. Jenkins passes `APP_VERSION`, `GIT_COMMIT` and `BUILD_DATE`, so any running pod traces back to a commit.
- Runs cleanly with `--read-only --tmpfs /tmp`. Kubernetes sets `readOnlyRootFilesystem: true`, drops all capabilities and applies `RuntimeDefault` seccomp.

```bash
docker build -t czarintel/api-service:dev docker/api-service
docker run --rm -p 8000:8000 --read-only --tmpfs /tmp czarintel/api-service:dev
curl -s localhost:8000/health | jq .
```

Tests: `pytest docker/api-service/tests` (Jenkins enforces ≥ 80% coverage).

## frontend

A static health dashboard served by **`nginxinc/nginx-unprivileged:1.27-alpine`** on port 8080 as UID 101.

- Build stage (`node:20-alpine`) minifies `index.html` with a pinned `html-minifier-terser@7.2.0`. Node never ships in the runtime image.
- Nginx config is a template: `API_UPSTREAM` (default `http://api-service:80`) is substituted at start. `/api/*` and `/health` are reverse-proxied with `X-Request-ID` propagation and tight timeouts.
- Security headers: `server_tokens off`, CSP (`default-src 'self'`, `frame-ancestors 'none'`), `X-Frame-Options DENY`, `nosniff`, strict `Referrer-Policy`, and `Permissions-Policy` denying geolocation, microphone and camera.
- `/healthz` for probes; SPA fallback `try_files $uri /index.html`.

```bash
docker build -t czarintel/frontend:dev docker/frontend
docker run --rm -p 8080:8080 -e API_UPSTREAM=http://host.docker.internal:8000 czarintel/frontend:dev
```

## Supply chain

```
Dockerfile ─► hadolint ─► Kaniko build (--reproducible, no daemon, no privileged pod)
          ─► Trivy image scan (CRITICAL gate) ─► crane push ─► ECR (IMMUTABLE tags, scan_on_push, KMS)
```

- **No Docker daemon in CI.** Kaniko builds unprivileged in the agent pod, so no `docker.sock` mount and no privileged containers (Gatekeeper and PSA would reject them anyway).
- ECR repositories are `IMMUTABLE` with `scan_on_push = true` and KMS encryption (Terraform AWS module). A tag can never be re-pointed at different bits.
- Base images are pinned by tag; Trivy fails the build if they carry fixable CRITICAL CVEs, which forces regular rebases.

For VM hosts that still run Docker Engine, see `configure-docker.yml` in the [Ansible guide](ansible.md). It applies CIS Docker Benchmark daemon settings: `userns-remap`, `no-new-privileges`, `icc: false`, `live-restore`, log rotation.

## Compliance mapping

| Control | Evidence |
|---|---|
| NIST CM-7 (least functionality), PCI-DSS 2.2.4 | Slim runtime, no compilers or package tooling, single process |
| NIST AC-6, CIS Docker 4.1 | Non-root UID 10001 / 101 |
| NIST SI-2, PCI-DSS 6.3.3 | OS upgrade at build, Trivy gate, scan-on-push |
| NIST SA-10, SR-4 | OCI provenance labels, reproducible builds, immutable tags |
| NIST SC-8 | Headers and proxying designed for TLS termination at the ingress |
