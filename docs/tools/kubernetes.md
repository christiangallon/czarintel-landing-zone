# Deep Dive: Kubernetes Baseline, OPA Gatekeeper and NetworkPolicies

**Location:** `kubernetes/` · **Targets:** EKS (prod), AKS (DR), GKE (data). The manifests are cloud-agnostic.

## Apply order

```bash
kubectl apply -f kubernetes/namespaces.yml                  # namespaces + PSA + quotas + limits
kubectl apply -f kubernetes/rbac/roles.yml                  # Entra-group-bound RBAC
kubectl apply -f kubernetes/network-policies/default-deny.yml
helm upgrade --install gatekeeper gatekeeper/gatekeeper -n gatekeeper-system --create-namespace --version 3.17.1
kubectl apply -f kubernetes/opa-gatekeeper/require-labels.yml
```

`scripts/bootstrap.sh cluster` runs exactly this sequence and retries the constraint apply until Gatekeeper has generated the constraint CRDs.

## 1. Namespaces (`namespaces.yml`)

| Namespace | `czarintel.io/environment` | PSA enforce | Extras |
|---|---|---|---|
| `production` | production | **restricted** (audit and warn restricted) | ResourceQuota, LimitRange, `istio-injection: enabled` (mesh-ready) |
| `staging` | staging | **restricted** | ResourceQuota, LimitRange |
| `monitoring` | shared | privileged (audit/warn **baseline**) | Documented exception: node-exporter needs host PID/network |

- The **LimitRange** injects default requests and limits, so a pod without resources still cannot starve a node.
- The **ResourceQuota** caps each namespace's total CPU, memory and object counts, which contains runaway deployments and noisy neighbours.

## 2. RBAC (`rbac/roles.yml`)
Humans never receive ServiceAccount tokens. Entra ID groups arrive in the OIDC token and bind to four ClusterRoles:

| ClusterRole | Binding | Can | Cannot |
|---|---|---|---|
| `cluster-admin` | `czarintel:platform-admins` (ClusterRoleBinding) | Everything | — |
| `czarintel-sre-operator` (aggregates `-base` and read-only) | `czarintel:sre` | Read all, `rollout restart`, scale, delete pods, exec/port-forward | Edit RBAC, CRDs, secrets |
| `czarintel-developer` | `czarintel:developers`: **RoleBinding in `staging`** | Manage workloads, services, configmaps in staging | Anything in production except read |
| `czarintel-read-only` | `czarintel:auditors` (cluster), developers in `production` | get/list/watch on non-sensitive resources | Read **Secrets**, exec |

`ci-deploy-reader` gives CI read-only visibility for rollout checks. CI never has write access to the cluster, because ArgoCD does the deploying.

## 3. NetworkPolicies (`network-policies/default-deny.yml`)
The pattern is to **deny everything, then allow by label**.

```
                 ingress-nginx ns
                        │ :8000
                        ▼
┌─────────── production ────────────────────────────┐
│  api-service ──:3306──► mysql                     │
│      │  ▲                     ▲                    │
│      │  └── :8000/metrics ── monitoring ns (Prometheus)
│      └──:443──► external APIs (egress)            │
│  all pods ──:53──► kube-dns                       │
└───────────────────────────────────────────────────┘
```

| Policy | Purpose |
|---|---|
| `default-deny-all` (production, staging, monitoring) | `podSelector: {}` with `policyTypes: [Ingress, Egress]` blocks all traffic |
| `allow-dns-egress` | UDP/TCP 53 to `kube-system` / `k8s-app: kube-dns` only |
| `allow-ingress-to-api` | From the `ingress-nginx` namespace to api-service port 8000 |
| `allow-prometheus-scrape` | From the `monitoring` namespace to metrics ports |
| `allow-api-to-mysql` + `mysql-accept-from-api` | **Both** directions declared, so egress and ingress are each explicit |
| `allow-api-https-egress` | TCP 443 to non-RFC1918 destinations (third-party APIs) |
| monitoring: `allow-intra-namespace`, `allow-ingress-to-grafana`, `allow-log-shipping-to-loki`, `allow-operator-webhook` | Keeps the observability stack functional under default-deny |

The Jenkins namespace policies live next to Jenkins in `ci-cd/jenkins/jenkins-deployment.yml`.

**Testing a policy:**
```bash
kubectl -n production run np-test --rm -it --image=busybox:1.36 --restart=Never \
  --labels=app.kubernetes.io/name=np-test -- wget -qO- -T 3 http://mysql:3306   # expect timeout
```

## 4. OPA Gatekeeper (`opa-gatekeeper/require-labels.yml`)
There are three ConstraintTemplates written in Rego, with four constraints, all set to `enforcementAction: deny`.

### `K8sRequiredLabels`
Validates that each label is present and matches its regex:
- `workloads-must-have-ownership-labels` applies to Deployments, StatefulSets and DaemonSets in production and staging. It requires `app.kubernetes.io/name`, `czarintel.io/owner` and `czarintel.io/cost-center` (`^CZ-[0-9]{4}$`).
- `namespaces-must-declare-environment` requires `czarintel.io/environment ∈ production|staging|shared|dev`, with system namespaces excluded.

### `K8sAllowedRepos` → `approved-registries-only`
Checks every container, initContainer and ephemeralContainer image against these prefixes:
- `…dkr.ecr.us-east-1.amazonaws.com/`
- `acrczarintelprod.azurecr.io/`
- `us-central1-docker.pkg.dev/czarintel-data-prod/`
- pinned `docker.io/library/mysql:`
- `quay.io/prometheus/`

### `K8sBlockLatestTag` → `block-latest-tag`
Rejects images tagged `:latest` and images with no tag at all.

**Rollout tip:** for a brownfield cluster, first set `enforcementAction: dryrun`. Then review the violations with `kubectl get k8srequiredlabels -o yaml` (`status.violations`) and switch to `deny`.

## 5. Where each control is verified

| Control | Admission (runtime) | CI (pre-merge) |
|---|---|---|
| Non-root, no privilege escalation, no host namespaces | PSA restricted | compliance_scan K8S-001/002/007 |
| Read-only root FS | Helm defaults | K8S-005 |
| Resource limits | LimitRange defaults | K8S-003 |
| Pinned image | Gatekeeper `block-latest-tag` | K8S-004 |
| Approved registry | Gatekeeper `approved-registries-only` | Code review |
| Default-deny present | — | K8S-006 |
| Ownership labels | Gatekeeper | Helm `_helpers.tpl` always renders them |
