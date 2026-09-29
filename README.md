<div align="center">

```
   ____                 ___       _       _
  / ___|______ _ _ __  |_ _|_ __ | |_ ___| |
 | |   |_  / _` | '__|  | || '_ \| __/ _ \ |
 | |___ / / (_| | |     | || | | | ||  __/ |
  \____/___\__,_|_|    |___|_| |_|\__\___|_|
        L A N D I N G   Z O N E
```

**Multi-cloud DevSecOps landing zone: Zero Trust at every layer, compliance as code, GitOps delivery.**

AWS (primary production) · Azure (identity and DR) · GCP (data and analytics)
Mapped to **HIPAA · SOC 2 · PCI-DSS 4.0 · NIST 800-53 rev5**

`Terraform 1.9.8` · `EKS / AKS / GKE` · `ArgoCD v2.12.6` · `Gatekeeper 3.17.1` · `kube-prometheus-stack 65.1.1` · `Loki 6.16.0` · `Fluent Bit 3.1.9`

</div>

---

## Why this exists

Regulated mid-market companies (healthtech, fintech, payments) keep running into the same problem. They need to ship weekly, and they need a clean SOC 2 / HIPAA / PCI audit. A typical platform build takes a 4–6 person team 9–12 months, and the result is usually a single-cloud setup with compliance evidence collected by hand in spreadsheets.

The CzarIntel Landing Zone is a **productised, opinionated baseline**. A senior consultant delivers it in **12 weeks**. The client team takes over at the end with everything in Git, every control mapped, and evidence generated automatically.

| Outcome | How the repo delivers it |
|---|---|
| **Audit-ready from day one** | 22 automated controls ([`compliance_scan.py`](scripts/compliance_scan.py)) mapped to HIPAA/SOC2/PCI/NIST. JSON evidence is produced on every PR and every night and kept for 400 days, which covers a full SOC 2 Type II window. |
| **No standing credentials** | People use SSO. CI uses OIDC. Workloads use IRSA, Azure Workload Identity or GKE Workload Identity. RDS uses a Secrets Manager–rotated password plus IAM auth. `bootstrap.sh` refuses to run with IAM user keys. |
| **Safe, fast change** | Jenkins builds, tests and scans (Trivy, hadolint, flake8), then commits an image tag. ArgoCD pulls the change. Rollback is `git revert`, and ArgoCD reconciles within about 3 minutes. |
| **Resilience with numbers** | RDS Multi-AZ plus 14-day PITR (RPO ≤ 5 min in-region). Warm AKS DR site (RTO ≤ 4 h cross-cloud). Both are tested through [runbooks](docs/runbooks/disaster-recovery.md). |
| **Cost accountability** | Every resource is tagged `CostCenter=CZ-1000`. Gatekeeper rejects workloads without `czarintel.io/cost-center`. [`cost_report.py`](scripts/cost_report.py) gates on budget overruns. |

### Business case (illustrative estimate)

These are planning estimates, not measured results. Validate them against the client's own figures in week 1.

| Item | Build in-house (typical) | CzarIntel engagement |
|---|---|---|
| Time to production-grade platform | 9–12 months | **12 weeks** |
| Delivery cost | ~4.5 FTE × 10 months ≈ $750K–$900K loaded | **$400K fixed scope** (C2C) |
| Audit preparation (SOC 2 Type II readiness) | $80K–$150K consultant plus spreadsheet evidence | Evidence pipeline included, and the control matrix is pre-mapped |
| Ongoing evidence collection | ~0.5 FTE | Automated (nightly CI) |

Assumptions: $170K–$200K loaded cost per senior platform engineer, and a single production workload family. Payback comes from **faster time-to-revenue** (enterprise deals blocked on SOC 2 or a HIPAA BAA) more than from engineering savings.

---

## Architecture

```
                          ┌─────────────────────────────────────────────────┐
                          │          Microsoft Entra ID (Azure AD)          │
                          │   Workforce SSO · MFA · Conditional Access      │
                          │   platform-admins · sre · developers · auditors │
                          └──────┬──────────────────┬───────────────────┬───┘
                     OIDC / SAML │             OIDC │                   │ Workload Identity Federation
                                 ▼                  ▼                   ▼
 ┌─────────────────────────────────────┐ ┌──────────────────────┐ ┌──────────────────────────┐
 │ AWS · PRIMARY PRODUCTION  us-east-1 │ │ AZURE · IDENTITY + DR│ │ GCP · DATA   us-central1 │
 │                                     │ │ eastus2              │ │                          │
 │  VPC 3×AZ  ─ private subnets ─ NAT  │ │  VNet + NSG          │ │  VPC + Cloud NAT         │
 │  ┌───────────── EKS ─────────────┐  │ │  AKS (Entra RBAC,    │ │  GKE private             │
 │  │ ns: production | staging      │  │ │   local accts off)   │ │   (Dataplane V2,         │
 │  │  api-service ◄─ ingress       │  │ │  Key Vault + CSI     │ │    Shielded, BinAuthZ)   │
 │  │  mysql (staging, StatefulSet) │  │ │  ACR (private)       │ │  Artifact Registry       │
 │  │ ns: monitoring                │  │ │  Log Analytics       │ │  BigQuery (CMEK)         │
 │  │  Prometheus · Alertmanager    │  │ │                      │ │  GCS backups (CMEK)      │
 │  │  Grafana · Loki · Fluent Bit  │  │ │   ▲ warm standby     │ │                          │
 │  │ ns: argocd | gatekeeper       │  │ │   │ same Helm charts │ │   ▲ de-identified        │
 │  │ default-deny NetworkPolicies  │  │ │   │ via ArgoCD       │ │   │ telemetry            │
 │  └───────────────────────────────┘  │ └───┼──────────────────┘ └───┼──────────────────────┘
 │  RDS MySQL 8.0 Multi-AZ (KMS, TLS,  │     │                        │
 │      IAM auth, PITR 14d)            │─────┘ failover (DNS)         │
 │  ECR (immutable, scan-on-push)      │──────────────────────────────┘
 │  GuardDuty → EventBridge → SNS      │
 │  CloudTrail (multi-region, KMS)     │
 │  S3 backups (KMS, versioned,        │
 │     lifecycle, public-block)        │
 └─────────────────────────────────────┘

  Delivery (pull-based, CI never holds cluster-admin):

  Developer ─PR─► GitHub ──► GitHub Actions: terraform fmt/validate · tflint · Checkov · Trivy config
                   │                          yamllint · ansible-lint · pytest · shellcheck · compliance_scan
                   └──────► Jenkins (K8s agents, IRSA): lint ► unit tests ► build ► Trivy image/fs/secrets
                                   ► push ECR ► helm lint ► commit image tag ──► Git (GitOps)
                                                                                   │
                            ArgoCD (OIDC SSO, RBAC, sync windows) ◄─────── pull ───┘
                                   │ staging auto-sync · production gated promotion
                                   ▼
                            EKS  (Gatekeeper admission: labels, non-root, pinned images)
```

Full walkthrough: [docs/architecture/overview.md](docs/architecture/overview.md) · Zero Trust model: [docs/architecture/zero-trust.md](docs/architecture/zero-trust.md)

---

## Technology stack

| Layer | Technology | Version | Role | Guide |
|---|---|---|---|---|
| IaC | Terraform (AWS/Azure/GCP modules) | 1.9.8 | All cloud infrastructure, S3 + DynamoDB state | [terraform.md](docs/tools/terraform.md) |
| Primary cloud | AWS: EKS, RDS, ECR, KMS, GuardDuty, CloudTrail | provider 5.x | Production runtime and data | [aws.md](docs/tools/aws.md) |
| Identity and DR | Azure: Entra ID, AKS, Key Vault, ACR | azurerm 3.x | Workforce identity, warm DR | [azure.md](docs/tools/azure.md) |
| Data | GCP: GKE, BigQuery, GCS, Artifact Registry | google 5.x | Analytics, de-identified telemetry | [gcp.md](docs/tools/gcp.md) |
| Orchestration | Kubernetes, OPA Gatekeeper | 1.29+, 3.17.1 | Workloads, admission policy | [kubernetes.md](docs/tools/kubernetes.md) |
| Packaging | Helm (`api-service`, `mysql`) | 3.16.2 | Versioned app releases | [helm.md](docs/tools/helm.md) |
| GitOps | ArgoCD | v2.12.6 | Pull-based delivery, promotion, DR retargeting | [argocd.md](docs/tools/argocd.md) |
| CI | Jenkins (K8s agents) plus GitHub Actions | LTS | Build, test, scan, push; IaC and quality gates | [jenkins.md](docs/tools/jenkins.md) |
| Containers | Docker multi-stage, Kaniko, Trivy | BuildKit 1.7 syntax | Non-root, pinned, scanned images | [docker.md](docs/tools/docker.md) |
| Config management | Ansible (CIS L1/L2) | ansible-core 2.17 | VM hardening, agents, Jenkins/MySQL hosts | [ansible.md](docs/tools/ansible.md) |
| Database | MySQL (RDS plus in-cluster StatefulSet) | 8.0.39 | Transactional store, nightly logical backups | [mysql.md](docs/tools/mysql.md) |
| Observability | Prometheus, Alertmanager, Grafana, Loki, Fluent Bit | 65.1.1 / 6.16.0 / 3.1.9 | Metrics, SLOs, 12 alerts, logs | [observability.md](docs/tools/observability.md) |
| Automation | Python 3.12 and Bash | n/a | Bootstrap, health, compliance, FinOps | [automation-scripts.md](docs/tools/automation-scripts.md) |
| Application | FastAPI service plus Nginx frontend | FastAPI 0.115 | Reference workload | [docker.md](docs/tools/docker.md) |

---

## Repository layout

```
czarintel-landing-zone/
├── terraform/                  # root module + modules/{aws,azure,gcp}; S3/DynamoDB backend
├── kubernetes/                 # namespaces, RBAC, default-deny NetworkPolicies, Gatekeeper constraints
├── helm/
│   ├── api-service/            # values-{staging,production}.yaml, HPA, Ingress, ServiceMonitor
│   └── mysql/                  # StatefulSet, exporter sidecar, nightly backup CronJob
├── argocd/                     # install (OIDC/RBAC/notifications), AppProject, Applications
├── ci-cd/                      # Jenkinsfile + Jenkins controller manifests (IRSA agents)
├── docker/                     # api-service (FastAPI) and frontend (unprivileged Nginx)
├── ansible/                    # CIS hardening, agents, docker/jenkins/mysql playbooks
├── monitoring/                 # kps values, 12 alert rules, 4 generated dashboards, Loki, Fluent Bit
├── scripts/                    # bootstrap.sh, health_check.py, compliance_scan.py, cost_report.py (+ tests)
├── .github/workflows/          # terraform-validate.yml, platform-quality.yml
└── docs/
    ├── architecture/           # overview, zero-trust
    ├── compliance/             # control matrix (HIPAA / SOC2 / PCI / NIST)
    ├── tools/                  # one guide per tool
    └── runbooks/               # incident-response, disaster-recovery
```

---

## Quickstart

**Prerequisites:** `terraform` ≥ 1.6, `aws` v2, `kubectl` ≥ 1.29, `helm` ≥ 3.14, `jq`, and an **AWS SSO session** (`aws sso login`). Static IAM user keys are rejected.

```bash
git clone https://github.com/czarintel/czarintel-landing-zone.git && cd czarintel-landing-zone
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # set account IDs, CIDRs, Entra group IDs

DRY_RUN=1 ./scripts/bootstrap.sh all     # review every mutating command first
./scripts/bootstrap.sh preflight         # toolchain + identity
./scripts/bootstrap.sh state             # state bucket + lock table (once per account)
./scripts/bootstrap.sh infra prod        # plan -> type 'apply' -> apply the saved plan
./scripts/bootstrap.sh cluster           # policies, Gatekeeper, monitoring, logging, ArgoCD

python3 scripts/health_check.py          # GREEN / AMBER / RED
python3 scripts/compliance_scan.py all --fail-on high
```

After `cluster` completes, ArgoCD reconciles `api-service` and `mysql`. Every later change goes through a pull request.

**Local validation (no cloud access needed):**

```bash
pip install -r scripts/requirements.txt
python -m pytest -q scripts/tests                              # 22 tests
python3 scripts/compliance_scan.py repo --fail-on medium       # 39/39 findings pass
terraform -chdir=terraform init -backend=false && terraform -chdir=terraform validate
helm lint --strict helm/api-service helm/mysql
```

---

## CI/CD quality gates

| Gate | Where | Blocks merge or deploy on |
|---|---|---|
| `terraform fmt -check`, `validate`, `tflint` | GH Actions `terraform-validate` | Formatting, invalid config, provider lint |
| Checkov and Trivy config (SARIF to code scanning) | GH Actions `terraform-validate` | IaC misconfiguration |
| `yamllint`, `ansible-lint`, `--syntax-check` | GH Actions `platform-quality` | YAML or Ansible errors |
| `pytest` (22), `shellcheck`, dashboard drift check | GH Actions `platform-quality` | Script regressions, stale Grafana JSON |
| `compliance_scan.py repo --fail-on medium` | GH Actions `platform-quality` | Any medium+ control failure (evidence kept 400 days) |
| `compliance_scan.py aws` and `cost_report.py` (nightly, OIDC) | GH Actions `platform-quality` | Critical account findings, with cost evidence |
| flake8, hadolint, unit tests | Jenkins | Code quality |
| Trivy image (HIGH/CRITICAL), Trivy fs and secrets | Jenkins | Vulnerable images, leaked secrets |
| `helm lint` | Jenkins | Broken charts |
| Gatekeeper admission | Cluster | Missing labels, root, unpinned images |
| ArgoCD sync windows and PR approval | GitOps | Unapproved production promotion |

---

## Compliance summary

Current repository scan: **100% (39/39 findings passing)** across 15 static controls. Seven more AWS account controls run nightly against the live account.

| Domain | Controls | HIPAA | SOC 2 | PCI-DSS | NIST 800-53 |
|---|---|---|---|---|---|
| Workload hardening | K8S-001…007 | 164.312(a)(1), (c)(1), (e)(1) | CC6.1, CC6.6, CC6.8, CC8.1 | 1.3, 2.2.x, 6.3.2 | AC-6, CM-7, SC-7, SI-7 |
| Supply chain | DOCKER-001…003 | 164.312(c)(1) | CC6.1, CC8.1, A1.2 | 2.2.5, 6.3.2, 10.6 | AC-6, CM-2, SI-4 |
| Infrastructure as code | TF-001…003 | 164.312(a)(2)(iv), (d) | CC6.1 | 3.5, 8.3.1 | SC-28, IA-5(7) |
| Host access | SSH-001…002 | 164.312(a)(1), (d) | CC6.1 | 8.2.1, 8.3 | AC-17, IA-2 |
| Cloud account | AWS-001…007 | 164.312(b), 164.308(a)(1) | CC6.1, CC6.6, CC7.2 | 1.3.1, 3.5, 8.4, 10.2, 11.5 | AU-2, SI-4, IA-2(1), SC-28 |

Full mapping, including organisational controls this repo cannot automate (BAAs, training, risk assessment): [docs/compliance/control-matrix.md](docs/compliance/control-matrix.md).

---

## Zero Trust summary

| Layer | Never trust, always verify |
|---|---|
| **Identity** | Entra ID SSO and MFA for people. OIDC federation for CI. IRSA and Workload Identity for pods. No long-lived keys anywhere. |
| **Network** | Private EKS/AKS/GKE nodes. Default-deny NetworkPolicies in every namespace with explicit allow-lists (19 policies). RDS reachable only from the node security group, TLS required. |
| **Workload** | Gatekeeper admission. Non-root, read-only root filesystem, no privilege escalation, dropped capabilities, pinned images. |
| **Data** | KMS/CMEK at rest on all three clouds. TLS in transit. Secrets in Secrets Manager or Key Vault through the CSI driver, never in Git or Terraform state. |
| **Delivery** | CI cannot reach clusters. ArgoCD pulls signed-off Git state. Every production change is a reviewed commit. |
| **Detection** | GuardDuty, CloudTrail, VPC Flow Logs, auditd and journald through Fluent Bit into Loki, 12 SLO and infrastructure alerts, and runbooks for each one. |

Details: [docs/architecture/zero-trust.md](docs/architecture/zero-trust.md)

---

## Operations

- **Alerts to runbooks:** each of the 12 alerts links to its section in [incident-response.md](docs/runbooks/incident-response.md).
- **Disaster recovery:** RDS PITR, logical dump restore, AKS failover through ArgoCD, in [disaster-recovery.md](docs/runbooks/disaster-recovery.md).
- **Dashboards** (`grafana.czarintel.io`): API SLO and Golden Signals · Multi-Cloud Cluster Overview · MySQL and ProxySQL · Zero Trust and Compliance Posture.

---

## 12-Week Delivery Roadmap

| Week | Phase | Deliverables | Exit criteria |
|---|---|---|---|
| **1** | Discover | Stakeholder interviews, data classification (PHI/PAN scope), threat model, `terraform.tfvars`, business-case validation | Signed scope and RACI; compliance targets agreed |
| **2** | Foundation | State backend, AWS org guardrails, Entra ID groups, SSO, GitHub OIDC roles | `bootstrap.sh preflight` + `state` green; no IAM users |
| **3** | Network and security | VPCs/VNet, KMS/CMEK, CloudTrail, GuardDuty → SNS, VPC Flow Logs | `compliance_scan.py aws` passes critical |
| **4** | Compute | EKS (primary), AKS (DR), GKE (data); namespaces, RBAC, default-deny policies, Gatekeeper | K8S-00x controls pass; admission denials tested |
| **5** | Data | RDS Multi-AZ + PITR, in-cluster MySQL for staging with nightly backups, BigQuery dataset | PITR test restore completed |
| **6** | CI | Jenkins on K8s (IRSA), Jenkinsfile, Trivy/hadolint gates, GitHub Actions IaC and quality gates | A failing CVE blocks the build |
| **7** | GitOps | ArgoCD with OIDC, AppProject, staging auto-sync, gated production promotion | Deploy and rollback via Git in < 5 min |
| **8** | Observability | kube-prometheus-stack, SLO rules, 12 alerts, 4 dashboards, Loki and Fluent Bit, PagerDuty/Slack routing | Alert fire-drill reaches on-call |
| **9** | Hardening | Ansible CIS L1/L2 for VM fleet, SSH certificates, compliance evidence pipeline (400-day retention) | 100% repo scan; evidence artifacts published |
| **10** | Resilience | **Automated cross-cloud logical dump** (RDS → S3 → Azure Blob WORM + GCS), `MySQLCrossCloudBackupStale` alert, Key Vault DR secrets | Newest Azure dump < 26 h; weekly restore test green |
| **11** | Integrations | **GitHub App installation tokens** replace PATs; **Slack OAuth app** replaces webhooks; FinOps budget gate in CI | No long-lived third-party tokens remain |
| **12** | Prove and hand over | DR game day (AKS failover), incident tabletop, runbook walkthroughs, knowledge transfer, audit-readiness report | Measured RTO/RPO within target; client team runs a deploy and a restore alone |

---

## Status and known gaps

- **Validated locally** (no cloud credentials were used):
  - `terraform validate` and `fmt`
  - `helm lint --strict` and kubeconform on all rendered manifests
  - `yamllint` and `ansible-lint`
  - Fluent Bit `--dry-run`
  - `pytest` (22 passing)
  - repo compliance scan at 39/39
- **Not yet applied to live accounts.** The AWS scanner, cost report and runbook CLI steps need real credentials and should be exercised in weeks 2–5.
- **Roadmap items that are not implemented yet:** automated cross-cloud dump (week 10); GitHub App and Slack OAuth (week 11).

---

<div align="center">

**CzarIntel** · Secure platforms for regulated industries
`CostCenter: CZ-1000` · © CzarIntel. All rights reserved.

</div>
