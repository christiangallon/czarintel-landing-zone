# Automation Scripts

> **CzarIntel Landing Zone** | Tool guide | Source: [`scripts/`](../../scripts)

Four operator tools cover the platform lifecycle: bootstrap, health, compliance evidence and FinOps. All of them follow the same rules:

- **No static credentials.** The Python tools use the ambient boto3 credential chain: AWS SSO locally, GitHub OIDC (`id-token: write`) in CI, IRSA in-cluster. `bootstrap.sh` refuses to run with IAM *user* credentials.
- **CI-friendly exit codes.** Every tool returns a documented exit code, so a pipeline can gate on it without parsing output.
- **Machine-readable output.** `--format json` is supported everywhere, and `-o` writes an artifact that is kept as audit evidence.
- **Tested.** `pytest scripts/tests` runs 22 tests with boto3 and HTTP mocked, so no cloud access is needed. `shellcheck` covers `bootstrap.sh`. Both run in the `platform-quality` GitHub Actions workflow.

```bash
pip install -r scripts/requirements.txt
python -m pytest -q scripts/tests
```

| Script | Purpose | Exit codes |
|---|---|---|
| `bootstrap.sh` | Toolchain preflight, state backend, Terraform, cluster add-ons, ArgoCD | `0` ok, `1` any failure (reports line and command) |
| `health_check.py` | HTTP/TCP/Prometheus synthetic checks with a RAG status | `0` GREEN, `1` AMBER, `2` RED or bad targets file |
| `compliance_scan.py` | 22 controls mapped to HIPAA/SOC2/PCI/NIST (repo and live AWS) | `0` pass, `1` findings at/above `--fail-on`, `3` scanner error |
| `cost_report.py` | Cost Explorer MTD/forecast/untagged spend, budget gate | `0` ok, `1` API error, `2` forecast over budget |

---

## 1. `bootstrap.sh`: zero to GitOps

```bash
./scripts/bootstrap.sh preflight            # tool versions + AWS identity (rejects IAM users)
./scripts/bootstrap.sh state                # S3 state bucket + DynamoDB lock table (once per account)
./scripts/bootstrap.sh infra   [workspace]  # terraform init / plan / apply (default: prod)
./scripts/bootstrap.sh cluster              # kubeconfig, namespaces, RBAC, policies, Gatekeeper, monitoring, ArgoCD
./scripts/bootstrap.sh all     [workspace]  # everything, in order

DRY_RUN=1 ./scripts/bootstrap.sh all        # print every mutating command without running it
```

| Command | What it does |
|---|---|
| `preflight` | Checks `terraform`, `aws`, `kubectl`, `helm` and `jq`. Calls `sts get-caller-identity` and stops if the ARN is `:user/` (long-lived keys break the Zero Trust policy). |
| `state` | Creates the versioned, KMS-encrypted, public-access-blocked S3 state bucket and the `czarintel-terraform-locks` DynamoDB table. Safe to re-run. |
| `infra` | Runs `terraform init`, selects or creates the workspace, writes `<workspace>.tfplan`, asks the operator to type `apply` to confirm, then applies exactly that saved plan. |
| `cluster` | Reads `aws_eks_cluster_name` from Terraform outputs, then applies in order: namespaces, RBAC, default-deny NetworkPolicies, Gatekeeper 3.17.1 (with retries for the constraint CRD race), kube-prometheus-stack 65.1.1 with rules and dashboards, Loki 6.16.0, Fluent Bit 0.47.10, ArgoCD v2.12.6, and finally the AppProject and Applications. |

Pinned versions can be overridden with environment variables (`KPS_CHART_VERSION`, `LOKI_CHART_VERSION`, `FLUENT_BIT_CHART_VERSION`, `GATEKEEPER_VERSION`, `ARGOCD_VERSION`, `AWS_REGION`, `LOCK_TABLE`). The script runs under `set -Eeuo pipefail`, and an `ERR` trap reports the failing line and command.

After `cluster` finishes, **ArgoCD owns application delivery**. Nobody runs `kubectl apply` against production for workloads after that point.

---

## 2. `health_check.py`: synthetic checks with a RAG status

Runs HTTP, TCP and Prometheus probes in parallel on a 16-worker thread pool. It needs only the standard library plus PyYAML, so it runs on any Jenkins agent, a laptop or a CronJob.

```bash
python3 scripts/health_check.py                                   # default: scripts/health_targets.yml
python3 scripts/health_check.py --format json > health.json
python3 scripts/health_check.py --url https://api.czarintel.io/health --url https://app.czarintel.io/
python3 scripts/health_check.py -t staging-targets.yml --timeout 3
```

**Checks per target type** (see [`health_targets.yml`](../../scripts/health_targets.yml)):

| Type | Assertions |
|---|---|
| `http` | `expect_status`, `expect_json` (subset match), `expect_text`. Going over `max_latency_ms` counts as **warn**, not fail. TLS is verified by default. `--insecure` exists for lab use only. |
| `tcp` | Connect within `--timeout` (for example ProxySQL on `6033`) |
| `prometheus` | Queries `/api/v1/alerts`. Firing alerts whose severity equals `fail_on_severity` (default `critical`) give **fail**. All other firing alerts give **warn**. `ignore_alerts` defaults to `Watchdog` and `InfoInhibitor`. |

**RAG and exit codes:** any `fail` gives `RED` / `2`, any `warn` gives `AMBER` / `1`, otherwise `GREEN` / `0`.

```
CzarIntel health: GREEN
------------------------------------------------------------------------
[OK  ] http   api-liveness                        41.3ms  200
[OK  ] tcp    proxysql                             2.1ms  connected
```

Where it is used: post-deploy verification after an ArgoCD sync, step 1 of every [incident runbook](../runbooks/incident-response.md), and the post-failover check in the [DR runbook](../runbooks/disaster-recovery.md).

---

## 3. `compliance_scan.py`: continuous compliance evidence

Two scanners share one findings model. Every control maps to **HIPAA, SOC2, PCI-DSS and NIST 800-53** (full matrix: [control-matrix.md](../compliance/control-matrix.md)).

```bash
python3 scripts/compliance_scan.py repo                                   # offline, static
python3 scripts/compliance_scan.py repo --fail-on medium --show-passed    # the CI gate
python3 scripts/compliance_scan.py aws --region us-east-1 --format json -o compliance-report-aws.json
python3 scripts/compliance_scan.py all --fail-on high
```

### Control catalogue (22 controls)

| ID | Control | Severity | NIST | PCI | SOC2 | HIPAA |
|---|---|---|---|---|---|---|
| K8S-001 | Containers run as non-root | high | AC-6, CM-7 | 2.2.5 | CC6.1 | 164.312(a)(1) |
| K8S-002 | No privileged containers or privilege escalation | critical | AC-6(10) | 2.2.5 | CC6.8 | 164.312(a)(1) |
| K8S-003 | CPU/memory limits | medium | SC-5 | 6.4 | A1.1 | 164.308(a)(7) |
| K8S-004 | Images pinned (no `:latest`) | high | CM-2, SI-7 | 6.3.2 | CC8.1 | 164.312(c)(1) |
| K8S-005 | Read-only root filesystem | medium | CM-7, SI-7 | 2.2 | CC6.8 | 164.312(c)(1) |
| K8S-006 | Default-deny NetworkPolicy per namespace | high | SC-7, AC-4 | 1.3 | CC6.6 | 164.312(e)(1) |
| K8S-007 | No hostNetwork/hostPID/hostIPC | critical | SC-39 | 2.2.1 | CC6.8 | 164.312(a)(1) |
| DOCKER-001 | Final stage runs as non-root `USER` | high | AC-6 | 2.2.5 | CC6.1 | 164.312(a)(1) |
| DOCKER-002 | Base images pinned | medium | CM-2 | 6.3.2 | CC8.1 | 164.312(c)(1) |
| DOCKER-003 | `HEALTHCHECK` defined | low | SI-4 | 10.6 | A1.2 | 164.308(a)(7) |
| TF-001 | Encrypted state backend | high | SC-28 | 3.5 | CC6.1 | 164.312(a)(2)(iv) |
| TF-002 | No hard-coded credentials | critical | IA-5(7) | 8.3.1 | CC6.1 | 164.312(d) |
| TF-003 | Storage/database encrypted at rest | high | SC-28 | 3.5 | CC6.1 | 164.312(a)(2)(iv) |
| SSH-001 | Root login disabled | high | AC-6(2), AC-17 | 8.2.1 | CC6.1 | 164.312(a)(1) |
| SSH-002 | Password authentication disabled | high | IA-2, IA-5 | 8.3 | CC6.1 | 164.312(d) |
| AWS-001 | Multi-region CloudTrail logging | critical | AU-2, AU-12 | 10.2 | CC7.2 | 164.312(b) |
| AWS-002 | GuardDuty enabled | high | SI-4 | 11.5 | CC7.2 | 164.308(a)(1)(ii)(D) |
| AWS-003 | S3 public access blocked | critical | AC-3, SC-7 | 1.3.1 | CC6.6 | 164.312(a)(1) |
| AWS-004 | S3 default encryption | high | SC-28 | 3.5 | CC6.1 | 164.312(a)(2)(iv) |
| AWS-005 | Root account MFA | critical | IA-2(1) | 8.4 | CC6.1 | 164.312(d) |
| AWS-006 | Access keys rotated within 90 days | medium | IA-5(1) | 8.3.9 | CC6.1 | 164.308(a)(5)(ii)(D) |
| AWS-007 | EBS encryption by default | high | SC-28 | 3.5 | CC6.1 | 164.312(a)(2)(iv) |

**Current repo result:** `score 100.0% (39/39 passed)`. The 39 findings are the 15 repo controls evaluated against every matching resource (containers, Dockerfiles, `.tf` files, namespaces, the SSH template). The AWS controls need a live account and run nightly.

Evidence flow in CI (`.github/workflows/platform-quality.yml`):

- **Every PR and push:** `repo --fail-on medium`. The JSON report is uploaded as an artifact and **kept for 400 days** (SOC2 Type II covers a 12-month observation window).
- **Nightly and on demand:** `aws --fail-on critical`, run through a read-only audit role assumed with OIDC (`vars.AWS_AUDIT_ROLE_ARN`). The cost report is uploaded alongside it.

---

## 4. `cost_report.py`: FinOps

Queries AWS Cost Explorer (`UnblendedCost`) and produces:

- month-to-date spend **by service** and **by cost-allocation tag** (`--tag-key`, default `CostCenter`; the platform's value is `CZ-1000`)
- a full-month **forecast** and a comparison with the previous month
- **untagged spend**, which counts as an accountability finding (SOC2 CC1.3)

```bash
python3 scripts/cost_report.py                                     # table
python3 scripts/cost_report.py --format csv -o reports/cost.csv    # finance hand-off
python3 scripts/cost_report.py --format json -o cost-report.json   # evidence / dashboards
python3 scripts/cost_report.py --budget 25000 --fail-over-budget   # CI/CD budget gate (exit 2)
python3 scripts/cost_report.py --tag-key Team --profile czarintel-finops
```

Cost Explorer is a global API served from `us-east-1`. The script always calls that region, whatever `AWS_REGION` is set to. Azure and GCP costs are out of scope for this script. Cost attribution is still enforced everywhere: Terraform applies a `CostCenter` default tag to cloud resources, and the Gatekeeper `require-labels` constraint requires `czarintel.io/cost-center` and `czarintel.io/owner` on workloads, so the data is ready for a multi-cloud FinOps export (see the roadmap in the [README](../../README.md#12-week-delivery-roadmap)).

---

## Related

- [Observability](observability.md): the alerts that `health_check.py` reads
- [Compliance control matrix](../compliance/control-matrix.md)
- [Incident response](../runbooks/incident-response.md) and [Disaster recovery](../runbooks/disaster-recovery.md)
