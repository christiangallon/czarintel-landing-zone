# Observability: Prometheus, Alertmanager, Grafana, Loki and Fluent Bit

> **CzarIntel Landing Zone** | Tool guide | Source: [`monitoring/`](../../monitoring), [`ansible/roles/node_exporter`](../../ansible/roles/node_exporter), [`ansible/roles/fluent_bit`](../../ansible/roles/fluent_bit)

A single, self-hosted observability plane in the `monitoring` namespace of the primary EKS cluster covers metrics, alerts, dashboards and logs for Kubernetes workloads **and** VMs. No telemetry leaves the tenancy. That matters for HIPAA, because logs can contain ePHI-adjacent metadata.

```
 Pods ──/metrics──► Prometheus (kps) ──► Alertmanager ──► PagerDuty (critical) / Slack (#czarintel-alerts)
 VMs ── node_exporter :9100 ──┘  │
                                  └─► Grafana (Entra ID SSO) ◄── Loki ◄── Fluent Bit DaemonSet (pods)
                                                                   ▲
                                          VM Fluent Bit ── mTLS ──┘  https://loki.czarintel.io
```

## Components and versions

| Component | Chart / version | Values | Installed by |
|---|---|---|---|
| Prometheus, Alertmanager, Grafana, node-exporter, kube-state-metrics | `kube-prometheus-stack` **65.1.1** (`fullnameOverride: kps`) | `monitoring/prometheus/values-override.yml` | `bootstrap.sh cluster` |
| Alert and recording rules | PrometheusRule `czarintel-api-alerts` | `monitoring/prometheus/custom-rules/api-alerts.yml` | `kubectl apply` in bootstrap |
| Dashboards | ConfigMap `czarintel-dashboards` (folder **CzarIntel**) | `monitoring/grafana/` (kustomize) | `kubectl apply -k` |
| Loki | `grafana/loki` **6.16.0**, SingleBinary plus gateway (2 replicas) | `monitoring/loki/values-override.yml` | bootstrap |
| Fluent Bit (cluster) | `fluent/fluent-bit` **0.47.10** (Fluent Bit 3.1.9) DaemonSet | `monitoring/fluent-bit/values-override.yml` | bootstrap |
| node_exporter / Fluent Bit (VMs) | node_exporter 1.8.2; Fluent Bit from the official repo | Ansible roles | `install-agents.yml` |

All four Helm releases render cleanly with Helm 3.16.2, and the output passes `kubeconform -strict`.

## Prometheus

- Retention **15 days / 45 GB** on a 50 Gi gp3 PVC. That covers alerting, incident review and weekly SLO reporting. Longer metric retention (Thanos or remote-write) is a client option, not part of the baseline.
- `serviceMonitorSelectorNilUsesHelmValues: false` (and the pod, rule and probe equivalents), so **any** ServiceMonitor or PrometheusRule in any namespace is discovered. App teams ship monitoring with their chart. The api-service chart ships its own ServiceMonitor.
- Scrape reachability respects Zero Trust. `production` and `staging` only admit ingress from pods labelled `app.kubernetes.io/name: prometheus` in `monitoring` (`allow-prometheus-scrape` NetworkPolicy). `monitoring` itself is ingress-default-deny.

## SLOs and alert rules

**SLO: 99.9% availability and p95 latency < 500 ms over 30 days** for api-service.

Recording rules (`czarintel-api.recording`):

- `job_namespace:http_requests:rate5m`
- `job_namespace:http_requests_5xx:rate5m`
- `job_namespace:http_error_ratio:rate5m`
- `job_namespace:http_request_duration_seconds:p95_5m`

| Alert | Severity | Fires when | Runbook |
|---|---|---|---|
| `HighErrorRate` | critical | 5xx ratio > 1% for 5m | [IR §4.1](../runbooks/incident-response.md#higherrorrate) |
| `HighLatency` | warning | p95 > 0.5 s for 5m | [IR §4.2](../runbooks/incident-response.md#highlatency) |
| `ErrorBudgetBurnFast` | critical | Burn rate > 14.4× over **both** 1h and 5m (budget gone in about 2 days) | [IR §4.3](../runbooks/incident-response.md#errorbudgetburnfast) |
| `ApiServiceDown` | critical | No healthy scrape target | [IR §4.4](../runbooks/incident-response.md#apiservicedown) |
| `PodCrashLooping` | critical | Container restarts in a loop | [IR §4.5](../runbooks/incident-response.md#podcrashlooping) |
| `DeploymentReplicasMismatch` | warning | Available ≠ desired replicas | [IR §4.6](../runbooks/incident-response.md#deploymentreplicasmismatch) |
| `HPAMaxedOut` | warning | HPA at `maxReplicas` | [IR §4.7](../runbooks/incident-response.md#hpamaxedout) |
| `NodeHighCPU` | warning | Node CPU saturation | [IR §4.8](../runbooks/incident-response.md#nodehighcpu) |
| `NodeDiskFillingUp` | warning | Disk predicted to fill | [IR §4.9](../runbooks/incident-response.md#nodediskfillingup) |
| `MySQLDown` | critical | mysqld-exporter reports down | [IR §4.10](../runbooks/incident-response.md#mysqldown) |
| `MySQLTooManyConnections` | warning | Connection saturation | [IR §4.11](../runbooks/incident-response.md#mysqltoomanyconnections) |
| `MySQLSlowQueries` | warning | Slow query rate rising | [IR §4.12](../runbooks/incident-response.md#mysqlslowqueries) |

Every rule carries `runbook_url`, `summary` and `description` annotations, so the page contains the next action.

## Alertmanager routing

| Match | Receiver | Notes |
|---|---|---|
| `severity="critical"` | `pagerduty-critical` | Routing key read from a mounted Secret file; never in values |
| `severity=~"critical\|warning"` | `slack-default` (`#czarintel-alerts`) | Webhook URL read from a Secret file |
| `alertname="Watchdog"` | `null` | Dead-man's switch; wire to an external heartbeat if desired |

`group_wait: 30s`, `repeat_interval: 4h`. Critical alerts **inhibit** warnings with the same labels, so there is one page per incident, not five. Alertmanager retention is 120h on a 5 Gi PVC. The Secret `alertmanager-receivers` is materialised from Key Vault.

## Grafana

- URL `https://grafana.czarintel.io` (ingress-nginx with a cert-manager `letsencrypt-prod` certificate).
- **Entra ID OAuth only.** `disable_login_form: true`. `role_attribute_path` maps `czarintel-platform-admins` → Admin, `czarintel-sre` → Editor, everyone else → Viewer.
- Datasources: Prometheus (default) and **Loki** (`uid: loki`, `http://loki-gateway.monitoring.svc.cluster.local`).
- Dashboards are **code**. `monitoring/grafana/generate_dashboards.py` emits deterministic JSON. `--check` fails CI if the committed JSON drifts from the generator.

| Dashboard | Panels | Highlights |
|---|---|---|
| `api-slo` | 21 | 30-day availability, error budget remaining, burn rate, latency percentiles, top and slowest endpoints, dependency health, HPA, api-service errors from Loki |
| `platform-overview` | 21 | Node readiness, failing pods, firing critical alerts, ArgoCD apps OutOfSync, CPU and memory commitment, per-node and per-namespace usage, restarts |
| `mysql` | 15 | Up and uptime, replication lag, connections used, buffer-pool hit ratio, slow queries, QPS, ProxySQL backend status and hostgroup routing |
| `security-posture` | 15 | Gatekeeper violations by constraint, denied admissions and API 403s, namespaces without default-deny, expiring TLS certs, auditd privilege escalation, SSH failures. Falco and running-image CVE panels populate once Falco and Trivy Operator are installed (roadmap week 7) |

```bash
python3 monitoring/grafana/generate_dashboards.py          # regenerate
python3 monitoring/grafana/generate_dashboards.py --check  # CI drift check
kubectl apply -k monitoring/grafana
```

## Logs: Loki and Fluent Bit

**Loki** (`monitoring/loki/values-override.yml`):

- SingleBinary with TSDB schema. **90-day retention** enforced by the compactor.
- Every pod meets the PSA *restricted* profile: non-root, read-only root FS, all capabilities dropped, `RuntimeDefault` seccomp.
- Gateway (2 replicas) serves in-cluster traffic at `loki-gateway.monitoring.svc`. An **mTLS ingress** `https://loki.czarintel.io` accepts pushes from VMs only when they present a client certificate signed by `monitoring/czarintel-logging-ca`, and only from `10.0.0.0/8`.
- ServiceMonitor label `release: kps`.

**Fluent Bit in the cluster** (`monitoring/fluent-bit/values-override.yml`):

- DaemonSet with `system-node-critical` priority, tolerating all taints.
- Tails `/var/log/containers/*.log` with CRI and docker multiline parsers. Offsets and a 1 GB filesystem buffer live on the node (`/var/lib/fluent-bit`), so restarts and Loki outages do not lose logs.
- The `kubernetes` filter merges the api-service JSON log body into structured fields. A `grep` filter drops `/health`, `/ready` and `/metrics` noise.
- Node-level logs are shipped too: the systemd journal for kubelet, containerd and sshd, plus `/var/log/audit/audit.log`. They carry the labels `job=fluent-bit-node`, `node` and `source` (`journal` or `auditd`). Managed Kubernetes nodes are excluded from the Ansible agent play, so this DaemonSet is their only shipper.
- Loki labels are deliberately low-cardinality: `job`, `cluster`, `namespace`, `container`, `pod`, `level`.
- The rendered configuration passes `fluent-bit --dry-run` (v3.1.9).
- Container hardening: read-only root FS, no privilege escalation, `drop: [ALL]`. It runs as UID 0 only because node log files are root-owned (monitoring namespace PSA exception, documented in [Zero Trust](../architecture/zero-trust.md)).

**Fluent Bit on VMs** (`ansible/roles/fluent_bit`): reads the systemd journal, **auditd** and sudo logs, plus any per-group `fluent_bit_extra_inputs`. On Kubernetes nodes that list is empty on purpose: the DaemonSet already ships container logs, so nothing is duplicated. It pushes to `loki.czarintel.io:443` over mTLS. The client cert, key and CA are copied from `FLUENT_BIT_TLS_DIR` on the control node, so they are never committed.

Useful LogQL:

```logql
{namespace="production", container="api-service"} | json | status >= 500
sum by (path) (rate({container="api-service"} | json | duration_ms > 500 [5m]))
{job=~"fluent-bit|fluent-bit-node"} |= "sshd" |= "Failed password"
```

## Compliance mapping

| Control | Evidence |
|---|---|
| HIPAA 164.312(b), PCI-DSS 10.2 / 10.7, NIST AU-2, AU-6, AU-11, AU-12 | Centralised logs, 90-day hot retention, auditd shipping |
| SOC2 CC7.2 / CC7.3, NIST SI-4, IR-5 | SLO alerts, PagerDuty paging, runbook links |
| SOC2 A1.2, NIST CP-2 | Availability SLO, error-budget tracking |
| NIST AC-2, IA-2 | SSO-only Grafana with group-to-role mapping |
| NIST SC-8, SC-23 | mTLS for off-cluster log ingestion |
