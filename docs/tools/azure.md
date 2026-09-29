# Deep Dive: Azure: Identity and Disaster Recovery

**Module:** `terraform/modules/azure/` · **Region:** `eastus2` (default) · **VNet:** `10.20.0.0/16`

## Role in the platform
1. **Identity plane.** Microsoft Entra ID is the single workforce identity provider for every cloud, Kubernetes cluster, Grafana and ArgoCD. Most regulated enterprises already run Entra ID with MFA and Conditional Access, so federating from it avoids a second identity store and a second offboarding process.
2. **Warm DR site.** AKS, Key Vault and ACR mirror the production runtime. If AWS us-east-1 fails, ArgoCD re-targets the same Helm charts to AKS (see the [DR runbook](../runbooks/disaster-recovery.md)).

## AKS hardening

| Setting | Value | Control |
|---|---|---|
| `local_account_disabled` | `true` | No shared cluster-admin kubeconfig. Every call is an Entra identity (IA-2). |
| `azure_active_directory_role_based_access_control` | `admin_group_object_ids = var.admin_group_object_ids` | Group-based RBAC (AC-2, AC-6) |
| `oidc_issuer_enabled` + `workload_identity_enabled` | `true` | Pods get Entra tokens through federation, with no client secrets |
| `azure_policy_enabled` | `true` | Azure Policy (Gatekeeper-based) add-on for platform guardrails |
| Network | Azure CNI **Overlay** + **Calico** NetworkPolicy | Same default-deny manifests as EKS work unchanged |
| API server | `api_authorized_ranges` allow list | Minimises exposure |
| `key_vault_secrets_provider` | Secret rotation enabled | Secrets Store CSI driver mounts Key Vault secrets as files |
| `microsoft_defender` | Log Analytics workspace | Runtime threat detection for containers |
| `sku_tier` | `Standard` in prod (financially backed SLA), `Free` elsewhere | Cost-aware |
| Node pools | System pool (`only_critical_addons_enabled`) and autoscaling user pool for workloads | Blast-radius separation |

## Key Vault
- `sku_name = "premium"`: HSM-backed keys (FIPS 140-2 Level 2).
- `purge_protection_enabled = true`, `soft_delete_retention_days = 90`. Nobody, not even a subscription owner, can permanently destroy keys before 90 days, which protects against ransomware and insider threats.
- Access policies per identity:
  - the deployer (Terraform)
  - the admin groups
  - the AKS CSI identity (read secrets)
  - the workload Entra application (get/list secrets only)
- Network ACL: `default_action = Deny` whenever `api_authorized_ranges` is set (always set it in prod), plus an `AzureServices` bypass.

## Workload Identity Federation

```
Pod (SA: api-service) ──projected token (iss = AKS OIDC issuer)──► Entra ID
     ▲                                                             │ federated credential
     └────────── Entra access token (1 h) ◄───────────────────────┘ azuread_application_federated_identity_credential
                          │
                          ▼
                      Key Vault
```

No client secret is ever created for the workload application, so there is nothing to leak or rotate.

## Observability
- `azurerm_log_analytics_workspace`, with diagnostic settings for AKS (kube-audit, kube-apiserver…), Key Vault (AuditEvent) and ACR (login/repository events).
- Retention is configurable. The logs feed Microsoft Sentinel if the client has it.

## ACR
- Private registry with `admin_enabled = false`. AKS pulls using its kubelet identity through an `AcrPull` role assignment.
- It is listed in the Gatekeeper `approved-registries-only` constraint.

## Entra ID groups → platform roles

| Entra group | Kubernetes (`kubernetes/rbac/roles.yml`) | Grafana (`role_attribute_path`) | ArgoCD project role |
|---|---|---|---|
| `czarintel:platform-admins` / `czarintel-platform-admins` | `cluster-admin` | Admin | ArgoCD admin (global RBAC) |
| `czarintel:sre` / `czarintel-sre` | `czarintel-sre-operator`: read all, rollout restart, scale, delete pods, exec/port-forward | Editor | — |
| `czarintel-release-managers` | — | Viewer | `release-manager`: sync and run actions on all apps, including production |
| `czarintel:developers` | `czarintel-developer` (edit) in staging, `czarintel-read-only` in production | Viewer | `developer`: view all, sync `*-staging` only |
| `czarintel:auditors` | `czarintel-read-only` (no Secrets) | Viewer | — |

Group claims are emitted by Entra ID in the OIDC token. Removing a user from a group revokes access everywhere at the next token refresh (≤ 1 h), which gives a single offboarding action.
