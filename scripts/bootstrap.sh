#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# CzarIntel Landing Zone bootstrap
#
#   ./scripts/bootstrap.sh preflight            # verify tooling + cloud identity
#   ./scripts/bootstrap.sh state                # create S3 state bucket + DynamoDB lock table (once per account)
#   ./scripts/bootstrap.sh infra   [workspace]  # terraform init/plan/apply (default workspace: prod)
#   ./scripts/bootstrap.sh cluster              # kubeconfig, namespaces, policies, Gatekeeper, monitoring, ArgoCD
#   ./scripts/bootstrap.sh all     [workspace]
#
# Identity: uses the caller's short-lived AWS SSO / OIDC session. No static keys.
# Set DRY_RUN=1 to print mutating commands instead of running them.
# -----------------------------------------------------------------------------
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${PROJECT:-czarintel}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LOCK_TABLE="${LOCK_TABLE:-czarintel-terraform-locks}"
KPS_CHART_VERSION="${KPS_CHART_VERSION:-65.1.1}"
LOKI_CHART_VERSION="${LOKI_CHART_VERSION:-6.16.0}"
FLUENT_BIT_CHART_VERSION="${FLUENT_BIT_CHART_VERSION:-0.47.10}"
GATEKEEPER_VERSION="${GATEKEEPER_VERSION:-3.17.1}"
ARGOCD_VERSION="${ARGOCD_VERSION:-v2.12.6}"
DRY_RUN="${DRY_RUN:-0}"

c_blue=$'\033[1;34m'; c_green=$'\033[1;32m'; c_red=$'\033[1;31m'; c_off=$'\033[0m'
log()  { printf '%s[czarintel]%s %s\n' "$c_blue" "$c_off" "$*"; }
ok()   { printf '%s[  ok  ]%s %s\n' "$c_green" "$c_off" "$*"; }
die()  { printf '%s[ fail ]%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }
trap 'die "line $LINENO: \"$BASH_COMMAND\" exited $?"' ERR

run() {
  if [[ "$DRY_RUN" == "1" ]]; then printf '  + %s\n' "$*"; else "$@"; fi
}

need() {
  local bin="$1" min="${2:-}"
  command -v "$bin" >/dev/null 2>&1 || die "missing required tool: $bin"
  ok "$bin $( { "$bin" version 2>/dev/null || "$bin" --version 2>/dev/null; } | head -1 | tr -d '\n' )${min:+ (need >= $min)}"
}

# ----------------------------------------------------------------------------
preflight() {
  log "Checking toolchain"
  need terraform 1.6
  need aws 2.0
  need kubectl 1.29
  need helm 3.14
  need jq
  log "Checking AWS identity"
  local ident
  ident="$(aws sts get-caller-identity --output json)" || die "no AWS session - run 'aws sso login'"
  ACCOUNT_ID="$(jq -r .Account <<<"$ident")"
  ok "AWS account $ACCOUNT_ID as $(jq -r .Arn <<<"$ident")"
  if jq -r .Arn <<<"$ident" | grep -q ':user/'; then
    die "long-lived IAM user credentials detected - use SSO/OIDC role sessions (Zero Trust policy)"
  fi
  export ACCOUNT_ID
}

# ----------------------------------------------------------------------------
bootstrap_state_backend() {
  [[ -n "${ACCOUNT_ID:-}" ]] || preflight
  local bucket="${PROJECT}-tfstate-${ACCOUNT_ID}"
  log "State bucket s3://$bucket, lock table $LOCK_TABLE ($AWS_REGION)"

  if aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
    ok "bucket exists"
  else
    if [[ "$AWS_REGION" == "us-east-1" ]]; then
      run aws s3api create-bucket --bucket "$bucket" --region "$AWS_REGION"
    else
      run aws s3api create-bucket --bucket "$bucket" --region "$AWS_REGION" \
        --create-bucket-configuration "LocationConstraint=$AWS_REGION"
    fi
  fi
  run aws s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled
  run aws s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"},"BucketKeyEnabled":true}]}'
  run aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  run aws s3api put-bucket-policy --bucket "$bucket" --policy "$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*",
"Action":"s3:*","Resource":["arn:aws:s3:::$bucket","arn:aws:s3:::$bucket/*"],
"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}
JSON
)"

  if aws dynamodb describe-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" >/dev/null 2>&1; then
    ok "lock table exists"
  else
    run aws dynamodb create-table --table-name "$LOCK_TABLE" --region "$AWS_REGION" \
      --attribute-definitions AttributeName=LockID,AttributeType=S \
      --key-schema AttributeName=LockID,KeyType=HASH \
      --billing-mode PAY_PER_REQUEST --sse-specification Enabled=true
    run aws dynamodb wait table-exists --table-name "$LOCK_TABLE" --region "$AWS_REGION"
    run aws dynamodb update-continuous-backups --table-name "$LOCK_TABLE" --region "$AWS_REGION" \
      --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true
  fi
  ok "state backend ready"
  export TF_STATE_BUCKET="$bucket"
}

# ----------------------------------------------------------------------------
infra() {
  local ws="${1:-prod}"
  [[ -n "${ACCOUNT_ID:-}" ]] || preflight
  local bucket="${PROJECT}-tfstate-${ACCOUNT_ID}"
  [[ -f "$ROOT/terraform/terraform.tfvars" ]] || die "copy terraform/terraform.tfvars.example to terraform.tfvars first"
  cd "$ROOT/terraform"
  log "terraform init (bucket=$bucket)"
  run terraform init -input=false -upgrade=false \
    -backend-config="bucket=$bucket" -backend-config="region=$AWS_REGION"
  run terraform workspace select -or-create "$ws"
  run terraform validate
  log "terraform plan ($ws)"
  run terraform plan -input=false -out="$ws.tfplan"
  if [[ "$DRY_RUN" != "1" ]]; then
    read -r -p "Apply plan to workspace '$ws'? type 'apply' to continue: " answer
    [[ "$answer" == "apply" ]] || die "aborted by operator"
  fi
  run terraform apply -input=false "$ws.tfplan"
  ok "infrastructure applied"
}

# ----------------------------------------------------------------------------
cluster() {
  cd "$ROOT/terraform"
  local name
  name="$(terraform output -raw aws_eks_cluster_name 2>/dev/null || true)"
  [[ -n "$name" && "$name" != "null" ]] || die "aws_eks_cluster_name output empty - run 'infra' first"
  log "Configuring kubeconfig for EKS cluster $name"
  run aws eks update-kubeconfig --name "$name" --region "$AWS_REGION" --alias "$PROJECT-prod"

  cd "$ROOT"
  log "Namespaces, RBAC and default-deny network policies"
  run kubectl apply -f kubernetes/namespaces.yml
  run kubectl apply -f kubernetes/rbac/roles.yml
  run kubectl apply -f kubernetes/network-policies/default-deny.yml

  log "OPA Gatekeeper $GATEKEEPER_VERSION"
  run helm repo add gatekeeper https://open-policy-agent.github.io/gatekeeper/charts --force-update
  run helm upgrade --install gatekeeper gatekeeper/gatekeeper -n gatekeeper-system --create-namespace \
    --version "$GATEKEEPER_VERSION" --wait
  # Constraints reference CRDs that Gatekeeper generates asynchronously from the
  # ConstraintTemplates, so the first apply can race; retry until established.
  local attempt
  for attempt in 1 2 3 4 5; do
    if run kubectl apply -f kubernetes/opa-gatekeeper/require-labels.yml; then
      break
    fi
    [[ "$attempt" -lt 5 ]] || die "Gatekeeper constraints failed to apply"
    log "waiting for constraint CRDs (attempt $attempt/5)"
    sleep 10
  done

  log "kube-prometheus-stack $KPS_CHART_VERSION + dashboards"
  run helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
  run helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
    -n monitoring --create-namespace --version "$KPS_CHART_VERSION" \
    -f monitoring/prometheus/values-override.yml --wait --timeout 10m
  run kubectl apply -f monitoring/prometheus/custom-rules/
  run kubectl apply -k monitoring/grafana

  log "Loki $LOKI_CHART_VERSION (log aggregation backend for Grafana)"
  run helm repo add grafana https://grafana.github.io/helm-charts --force-update
  run helm upgrade --install loki grafana/loki -n monitoring \
    --version "$LOKI_CHART_VERSION" -f monitoring/loki/values-override.yml --wait --timeout 10m

  log "Fluent Bit $FLUENT_BIT_CHART_VERSION (node log shipper -> Loki)"
  run helm repo add fluent https://fluent.github.io/helm-charts --force-update
  run helm upgrade --install fluent-bit fluent/fluent-bit -n monitoring \
    --version "$FLUENT_BIT_CHART_VERSION" -f monitoring/fluent-bit/values-override.yml --wait --timeout 5m

  log "ArgoCD $ARGOCD_VERSION (GitOps takes over application delivery from here)"
  run kubectl apply -f argocd/install/argocd-namespace.yml
  run kubectl apply -n argocd --server-side \
    -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
  run kubectl -n argocd rollout status deploy/argocd-server --timeout=300s
  run kubectl apply -f argocd/projects/czarintel-project.yml
  run kubectl apply -f argocd/applications/
  ok "cluster bootstrapped - ArgoCD now reconciles api-service and mysql"
}

usage() { sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    preflight) preflight ;;
    state)     preflight; bootstrap_state_backend ;;
    infra)     preflight; infra "${1:-prod}" ;;
    cluster)   cluster ;;
    all)       preflight; bootstrap_state_backend; infra "${1:-prod}"; cluster ;;
    -h|--help|help|"") usage ;;
    *) usage; die "unknown command: $cmd" ;;
  esac
}

main "$@"
