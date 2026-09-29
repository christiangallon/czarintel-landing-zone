# ArgoCD: GitOps Delivery

> **CzarIntel Landing Zone** | Tool guide | Source: [`argocd/`](../../argocd)

ArgoCD is the **only** way workloads reach a cluster. CI never holds cluster credentials. Jenkins pushes an image to an immutable ECR tag, then commits that tag (with the digest in the commit body) to Git. ArgoCD pulls that change and reconciles it. Every production change therefore has a Git commit, a reviewer and a timestamp, which covers SOC2 CC8.1, PCI-DSS 6.5.1 and NIST CM-3.

## Layout

| Path | Purpose |
|---|---|
| `argocd/install/argocd-namespace.yml` | Namespace (PSA `baseline` enforce, `restricted` warn), `argocd-cm` (Entra ID OIDC, local admin and anonymous disabled, `exec` disabled), `argocd-rbac-cm`, `argocd-cmd-params-cm`, `argocd-notifications-cm` |
| `argocd/projects/czarintel-project.yml` | `czarintel` AppProject: the tenancy and blast-radius boundary |
| `argocd/applications/api-service-app.yml` | `api-service-staging` and `api-service-production` Applications (Helm chart `helm/api-service`) |
| `argocd/applications/mysql-app.yml` | In-cluster MySQL (Helm chart `helm/mysql`) for staging |

Version pinned in `scripts/bootstrap.sh`: `ARGOCD_VERSION=v2.12.6`. Bootstrap applies the standard manifest with `--server-side`. Production clusters with three or more nodes should use `manifests/ha/install.yaml` (the header of `argocd-namespace.yml` shows it): Redis HA and multiple replicas of the repo server and API server.

## Install

```bash
# Idempotent: part of `scripts/bootstrap.sh cluster`
kubectl apply -f argocd/install/argocd-namespace.yml
kubectl apply -n argocd --server-side \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.12.6/manifests/install.yaml   # or ha/install.yaml
kubectl apply -f argocd/projects/ -f argocd/applications/
```

The OIDC placeholders `$oidc.azure.tenantId`, `$oidc.azure.clientId` and `$oidc.azure.clientSecret`, plus the Slack `$slack-token`, resolve from the `argocd-secret` / `argocd-notifications-secret` Secrets. Those Secrets are synced from Azure Key Vault by External Secrets and are never committed.

## Identity and RBAC (Zero Trust)

| Entra ID group | ArgoCD role | Permissions |
|---|---|---|
| `czarintel-platform-admins` | `role:admin` | Full control |
| `czarintel-sre` | `role:sre` | get / sync / actions / logs on all apps; cannot edit projects or clusters |
| `czarintel-release-managers` | project role `release-manager` | get / sync / actions on all `czarintel` apps, including production |
| `czarintel-developers` | project role `developer` | Sync `*-staging` apps only |
| `czarintel-auditors` | `role:readonly` | Read only (this is also `policy.default`) |
| *(automation)* | project role `ci` | Read-only token for status checks |

Local `admin` is disabled after SSO bootstrap (`admin.enabled: "false"`) and anonymous access is off. `exec.enabled: "false"` removes the web-terminal attack surface.

## AppProject guardrails

`czarintel` AppProject enforces:

- **sourceRepos**: only `czarintel-landing-zone`. Arbitrary charts cannot be deployed.
- **destinations**: `production`, `staging` and `monitoring` namespaces only.
- **clusterResourceWhitelist**: `Namespace` only. Apps cannot create ClusterRoles, CRDs or webhooks.
- **namespaceResourceBlacklist**: `ResourceQuota`, `LimitRange`, `NetworkPolicy`. Platform-owned guardrails cannot be weakened by an app.
- **orphanedResources: warn**: surfaces drift created out-of-band (`kubectl apply` by hand).
- **syncWindows**: `deny` on `*-production` from **Friday 18:00 America/New_York for 60 hours**. `manualSync: true` keeps the break-glass path for release managers.

## Application sync policy

| Setting | Staging | Production | Why |
|---|---|---|---|
| `automated.prune` / `selfHeal` | on | on | Git is the source of truth; manual drift is reverted |
| `ServerSideApply=true` | ✔ | ✔ | Field ownership, large CRDs |
| `ApplyOutOfSyncOnly=true` | ✔ | ✔ | Faster, quieter syncs |
| `PruneLast=true` | ✔ | ✔ | New ReplicaSet is healthy before old objects are removed |
| retry | 5 × backoff 10s→3m (factor 2) | same | Absorbs transient API errors |
| `ignoreDifferences /spec/replicas` | ✔ | ✔ | HPA owns replica count |
| Slack notifications | all three triggers → `#czarintel-deploys` | sync-failed and degraded → `#czarintel-prod-alerts`; deployed → `#czarintel-deploys` | Failures page the right channel |

`mysql-app` deliberately uses **`prune: false`**. A mistaken Git deletion must never delete a StatefulSet or its PVC. Credentials come from `existingSecret: mysql-credentials`, which External Secrets materialises from Key Vault.

## Promotion flow

```
Jenkins (main)  ──► ECR push (immutable tag) ──► commit values-staging.yaml ──► ArgoCD syncs staging
Release manager ──► Jenkins TARGET_ENV=production ──► commit values-production.yaml
                ──► ArgoCD syncs production (blocked Fri 18:00 → Mon 06:00 unless manual)
```

Rollback: `git revert` the promotion commit. ArgoCD re-syncs the previous image tag in about 3 minutes (`timeout.reconciliation: 180s`). `argocd app rollback api-service-production <ID>` also works for SRE, but auto-sync re-applies Git state afterwards, so the Git revert is the durable fix.

## Disaster recovery role

The same Applications can target the Azure AKS DR cluster: register it with `argocd cluster add` and add a destination. See [Disaster Recovery](../runbooks/disaster-recovery.md).

## Compliance mapping

| Control | Evidence |
|---|---|
| SOC2 CC8.1 / NIST CM-3 | Every deploy is a reviewed Git commit; ArgoCD history per revision |
| NIST CM-2 / CM-6 | `selfHeal` enforces the baseline continuously |
| NIST AC-2 / AC-6, HIPAA 164.312(a)(1) | SSO-only access, group-mapped least-privilege roles |
| PCI-DSS 6.5.1 | Change windows, approvals, rollback via revert |
| NIST AU-12 | Sync history plus Slack notification audit trail |

## Troubleshooting

| Symptom | Check |
|---|---|
| App `OutOfSync` forever on replicas | Confirm the `ignoreDifferences` block is present |
| `permission denied` on sync | User's Entra group claim: `argocd account get-user-info` |
| Production sync refused | Sync window is active; use manual sync with release-manager approval |
| `resource ... is not permitted in project` | Resource type is blocked by the AppProject whitelist or blacklist; this is intended |
