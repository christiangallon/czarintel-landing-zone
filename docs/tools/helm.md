# Deep Dive: Helm Charts

**Location:** `helm/` · **Helm:** 3.16.2 (pinned in the Jenkins agent image) · **Charts:** `api-service`, `mysql`

## Why Helm (and not raw manifests or Kustomize)
- **Values layering** (`values.yaml` → `values-<env>.yaml`) keeps one template and tiny, reviewable per-environment diffs.
- **ArgoCD renders Helm natively.** No Tiller and no `helm install` in production: ArgoCD runs `helm template` and applies the result, so Git remains the source of truth.
- `helm lint` and `helm template` in CI catch schema and templating errors before merge.

## `api-service` chart

```
helm/api-service/
├── Chart.yaml
├── values.yaml              # secure defaults (dev)
├── values-staging.yaml      # 2 replicas, HPA 2→4 @ 75% CPU, letsencrypt-staging
├── values-production.yaml   # 4 replicas, HPA 4→20 @ 65% CPU / 75% mem, PDB minAvailable 3, anti-affinity
└── templates/
    ├── _helpers.tpl         # names + mandatory czarintel.io/* labels
    ├── deployment.yaml      # Deployment + PodDisruptionBudget
    ├── service.yaml  configmap.yaml  serviceaccount.yaml
    ├── hpa.yaml             # autoscaling/v2, CPU + memory targets
    ├── ingress.yaml         # TLS via cert-manager
    └── servicemonitor.yaml  # Prometheus Operator scrape config
```

### Secure-by-default pod spec
| Field | Value |
|---|---|
| `securityContext` (pod) | `runAsNonRoot: true`, `runAsUser: 10001`, `fsGroup`, `seccompProfile: RuntimeDefault` |
| `securityContext` (container) | `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]` |
| `automountServiceAccountToken` | `false`. IRSA's projected token is injected separately by the EKS webhook. |
| Writable paths | `emptyDir` mounted at `/tmp` only |
| Probes | `startupProbe` (slow cold starts), `livenessProbe` on `/health`, `readinessProbe` on `/ready` (checks dependencies) |
| Lifecycle | `preStop` sleep plus `terminationGracePeriodSeconds: 30`, so in-flight requests drain before endpoints are removed |
| Rollout | `RollingUpdate` with `maxUnavailable: 0`: capacity never drops during a deploy |
| Spread | `topologySpreadConstraints` across zones. Production adds hostname anti-affinity. |
| Config rollout | `checksum/config` annotation, so a ConfigMap change triggers a rolling restart |

### Production ingress
- Host `api.czarintel.io` with a TLS certificate from cert-manager (`letsencrypt-prod`) and forced HTTPS redirect.
- `limit-rps: 50` per client IP, plus **ModSecurity with the OWASP Core Rule Set** as an in-cluster WAF layer.
- An HSTS header with preload is set through a configuration snippet. This requires ingress-nginx `allow-snippet-annotations: true`. Alternatively, use the controller's global `hsts` settings, which are on by default.

### Secrets
The chart never contains secret values. `existingSecret` references a Secret synced from AWS Secrets Manager (for example by External Secrets Operator) using the api-service IRSA role, which is scoped to that single secret ARN.

## `mysql` chart (staging and self-managed use)
Production uses **RDS**. This chart covers staging, ephemeral preview environments and the Azure DR cluster.

| Feature | Implementation |
|---|---|
| StatefulSet plus `volumeClaimTemplates`, pinned `mysql:8.0.39` | Stable identity and storage |
| Sidecar `mysqld-exporter v0.15.1` | Metrics feed the MySQL dashboard and alerts |
| Passwords | Generated on first install, preserved on upgrade through `lookup`. Or set `auth.existingSecret`. |
| `helm.sh/resource-policy: keep` on Secret and PVC | `helm uninstall` or an ArgoCD prune can never delete data or credentials |
| `automountServiceAccountToken: false`, non-root, RO root FS with explicit writable mounts | Same hardening bar as stateless apps |
| ArgoCD Application with **automated prune disabled** | Deleting stateful resources requires a human |

## Commands

```bash
helm lint helm/api-service -f helm/api-service/values-production.yaml
helm template api-service helm/api-service \
  -f helm/api-service/values.yaml -f helm/api-service/values-production.yaml \
  --set image.tag=1.4.2 | kubectl apply --dry-run=server -f -
```

## Promotion model
Jenkins updates only `image.tag` (and the registry path) in `values-<TARGET_ENV>.yaml`. `TARGET_ENV` is a pipeline parameter that defaults to `staging`, and the update runs only for `main` and `release/*` builds. That one-line Git diff, whose commit message records the source SHA, the image digest and the build URL, is the entire change record for a deployment.
