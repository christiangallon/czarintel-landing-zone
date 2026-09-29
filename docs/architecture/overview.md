# CzarIntel Landing Zone — Architecture Overview

> **Audience:** CTOs, security architects, auditors and platform engineers.
> **Scope:** every component in this repository, how the components connect, and why each design decision was made.

---

## 1. Design principles

| # | Principle | How it is enforced in this repo |
|---|-----------|---------------------------------|
| P1 | **Zero Trust: never trust, always verify** | Default-deny NetworkPolicies, OIDC/workload identity on every cloud, no static credentials, mTLS-ready service boundaries. See [zero-trust.md](zero-trust.md). |
| P2 | **Everything as code** | Terraform (infra), Helm (apps), ArgoCD (delivery), Ansible (VMs), Gatekeeper (policy), Python-generated Grafana dashboards. Nothing is configured through a console. |
| P3 | **Pull-based delivery** | Jenkins builds, scans and pushes images, then bumps a Git tag. ArgoCD pulls that change into the cluster. CI never holds cluster-admin credentials. |
| P4 | **Right cloud for the job** | AWS runs production, Azure provides identity and DR, GCP runs data/ML. Each cloud is used for what it does best, not duplicated. |
| P5 | **Compliance is a build artefact** | `scripts/compliance_scan.py` runs in CI and fails the build on regressions. Every control is mapped in [../compliance/control-matrix.md](../compliance/control-matrix.md). |
| P6 | **Observable by default** | Every workload exposes Prometheus metrics, ships logs to Loki, and is covered by an SLO dashboard and burn-rate alerts. |

---

## 2. Multi-cloud role allocation

```
                        ┌───────────────────────────────────────────┐
                        │        Microsoft Entra ID (Azure AD)      │
                        │  Workforce identity · MFA · Cond. Access  │
                        │  Groups: platform-admins, sre, developers,│
                        │          auditors  → K8s RBAC + Grafana   │
                        └───────┬───────────────┬───────────────┬───┘
                     OIDC / SAML│               │OIDC           │ Workload Identity
                                ▼               ▼               ▼ Federation
┌──────────────────────────────────┐ ┌─────────────────────┐ ┌──────────────────────────┐
│ AWS — PRIMARY PRODUCTION         │ │ AZURE — IDENTITY+DR │ │ GCP — DATA / ML          │
│ us-east-1 · 10.10.0.0/16         │ │ eastus2·10.20.0.0/16│ │ us-central1·10.30.0.0/20 │
│                                  │ │                     │ │                          │
│ VPC 3×AZ public/private + NAT    │ │ VNet + NSG          │ │ VPC + Cloud NAT          │
│ EKS (private nodes, IMDSv2,      │ │ AKS (Entra RBAC,    │ │ GKE private (Dataplane V2│
│      KMS secret envelope, IRSA)  │ │  local accts OFF,   │ │  Workload Identity,      │
│ RDS MySQL Multi-AZ (KMS, TLS-only│ │  Workload Identity, │ │  Shielded nodes, BinAuthZ│
│      IAM auth, PI)               │ │  Calico, Defender)  │ │  CMEK secrets)           │
│ ECR (immutable tags, scan-on-push│ │ Key Vault (purge    │ │ Artifact Registry        │
│ GuardDuty (EKS/RDS/S3/Runtime)   │ │  protection, CSI)   │ │ BigQuery (CMEK, partition│
│   → EventBridge → SNS            │ │ ACR (private)       │ │  expiry, dataset IAM)    │
│ S3 backups (KMS, versioned,      │ │ Log Analytics       │ │ GCS backups (CMEK,       │
│   TLS-only, public-block)        │ │                     │ │  versioned, soft delete) │
│ VPC Flow Logs · CloudTrail (KMS) │ │                     │ │                          │
└──────────────┬───────────────────┘ └──────────┬──────────┘ └────────────┬─────────────┘
               │           Failover (DNS)       │   De-identified telemetry│
               └────────────────────────────────┘◄─────────────────────────┘
```

| Cloud | Role | Why this cloud |
|-------|------|----------------|
| **AWS** | Primary production: VPC, EKS, RDS, GuardDuty, ECR, S3 | Deepest managed-service catalogue and the most mature HIPAA BAA / PCI attestation coverage. GuardDuty adds EKS runtime threat detection. |
| **Azure** | Workforce identity (Entra ID) plus warm DR (AKS, Key Vault, ACR) | Most enterprise buyers already run Entra ID. Federating from it avoids a second identity silo, and AKS in eastus2 gives geographic and provider diversity for DR. |
| **GCP** | Data and ML: GKE, BigQuery, Artifact Registry | BigQuery is serverless and has column-level governance. GKE Autopilot-grade hardening (Binary Authorization, Dataplane V2) suits batch/ML workloads. |

Each cloud is behind its own feature flag (`enable_aws`, `enable_azure`, `enable_gcp`), so a client can adopt one cloud at a time.

---

## 3. Software delivery flow

```
 Developer ──PR──► GitHub ──webhook──► Jenkins (EKS, ephemeral K8s agents)
                    │                     │
                    │                     ├─ flake8 ∥ hadolint
                    │                     ├─ pytest + coverage
                    │                     ├─ Kaniko build (rootless, no Docker socket)
                    │                     ├─ Trivy image CVE ∥ Trivy fs + secret scan  ── gate: HIGH/CRITICAL
                    │                     ├─ crane push → ECR (IRSA, immutable tag + digest)
                    │                     ├─ helm lint/template with the promoted tag
                    │                     └─ git commit image tag → GitOps repo
                    │
  GitHub Actions ───┤  terraform fmt/validate/tflint/checkov · ansible-lint
                    │  pytest (scripts) · shellcheck · compliance_scan repo
                    │  nightly: compliance_scan aws + cost_report (OIDC)
                    ▼
                 ArgoCD (argocd ns) ── detects tag change ──► sync staging (auto)
                    │                                        ► sync production (auto, self-heal)
                    ▼
            OPA Gatekeeper admission ── rejects: missing labels, non-approved registry, :latest
                    ▼
            Pod Security Admission "restricted" ── rejects: root, privileged, hostPath …
                    ▼
              Running workload ── metrics → Prometheus → Grafana / Alertmanager
                                 └ logs    → Fluent Bit → Loki → Grafana
```

---

## 4. Repository map

| Path | Layer | Deep dive |
|------|-------|-----------|
| `terraform/` | Multi-cloud infrastructure (root plus `aws`, `azure`, `gcp` modules) | [tools/terraform.md](../tools/terraform.md), [tools/aws.md](../tools/aws.md), [tools/azure.md](../tools/azure.md), [tools/gcp.md](../tools/gcp.md) |
| `docker/` | Hardened api-service (FastAPI) and frontend images | [tools/docker.md](../tools/docker.md) |
| `kubernetes/` | Namespaces with PSA, RBAC, NetworkPolicies, Gatekeeper | [tools/kubernetes.md](../tools/kubernetes.md) |
| `helm/` | `api-service` and `mysql` charts with per-env values | [tools/helm.md](../tools/helm.md) |
| `argocd/` | AppProject plus Applications (GitOps) | [tools/argocd.md](../tools/argocd.md) |
| `ci-cd/` | Jenkinsfile and Jenkins-on-Kubernetes manifests | [tools/jenkins.md](../tools/jenkins.md) |
| `.github/workflows/` | IaC and platform quality gates | [tools/jenkins.md#github-actions-quality-gates](../tools/jenkins.md#github-actions-quality-gates) |
| `monitoring/` | kube-prometheus-stack, alert rules, Loki, Fluent Bit, Grafana dashboards-as-code | [tools/observability.md](../tools/observability.md) |
| `ansible/` | CIS hardening, agents, Docker, Jenkins, MySQL/ProxySQL on VMs | [tools/ansible.md](../tools/ansible.md), [tools/mysql.md](../tools/mysql.md) |
| `scripts/` | Bootstrap, compliance scanner, cost report, health check | [tools/automation-scripts.md](../tools/automation-scripts.md) |
| `docs/` | This documentation | — |

---

## 5. Environments

| Environment | Terraform workspace | K8s namespace | Helm values | ArgoCD sync |
|-------------|--------------------|---------------|-------------|-------------|
| dev | `dev` | `staging` (shared) | `values.yaml` | manual |
| staging | `stg` | `staging` | `values-staging.yaml` (2–4 replicas) | automated |
| production | `prod` | `production` | `values-production.yaml` (4–20 replicas, HPA 65% CPU) | automated plus self-heal |

State lives in one S3 bucket (versioned, KMS) with DynamoDB locking. Workspaces keep keys separate under `env:/<workspace>/`.

---

## 6. Non-functional targets

| Attribute | Target | Mechanism |
|-----------|--------|-----------|
| Availability SLO | 99.9% monthly (43 min error budget) | Multi-AZ EKS node group, RDS Multi-AZ, HPA, PDB, multi-window burn-rate alerts |
| Latency SLO | p95 < 500 ms | `HighLatency` alert, API SLO dashboard |
| RPO | ≤ 5 min in-region, ≤ 24 h cross-cloud | RDS Multi-AZ synchronous standby plus PITR. Cross-cloud: daily logical dump to the Azure DR site ([DR runbook](../runbooks/disaster-recovery.md); scheduling it is roadmap week 10) |
| RTO | ≤ 1 h regional, ≤ 4 h cross-cloud | Terraform re-apply, ArgoCD re-sync to AKS; see [../runbooks/disaster-recovery.md](../runbooks/disaster-recovery.md) |
| Patch latency | Critical CVEs < 72 h | Trivy gate, ECR scan-on-push, managed node AMI rollouts, Ansible `harden.yml` |
| Audit evidence | Continuous | Multi-region CloudTrail (log-file validation, KMS, 6-year retention), EKS audit logs, Azure diagnostics, Loki 90-day retention, nightly compliance scan artefacts |
