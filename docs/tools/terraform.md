# Deep Dive: Terraform (Multi-Cloud IaC)

**Location:** `terraform/` · **Terraform:** `>= 1.6, < 2.0` (CI pins 1.9.8)
**Providers:** aws `~> 5.60`, azurerm `~> 3.110`, azuread `~> 2.53`, google `~> 5.40`, tls `~> 4.0`, random `~> 3.6`

## Why Terraform
- **One language for three clouds.** One engineer can review an AWS VPC, an AKS cluster and a BigQuery dataset in the same PR.
- **Plan before apply.** Every change produces a reviewable diff, which is the change-control evidence for SOC 2 CC8.1 and PCI 6.5.1.
- **Ecosystem.** checkov, tflint and Infracost integrate natively.

## Layout

```
terraform/
├── versions.tf               # version pins + provider config (default_tags on AWS)
├── backend.tf                # S3 remote state + DynamoDB locking + workspaces
├── variables.tf              # every input typed, described and validated
├── main.tf                   # locals (name_prefix, common_tags) + 3 feature-flagged modules
├── outputs.tf                # values consumed by bootstrap.sh, Helm and ArgoCD
├── terraform.tfvars.example  # copy to terraform.tfvars (gitignored)
└── modules/
    ├── aws/    VPC, EKS, IRSA, RDS, ECR, S3 backups, CloudTrail, GuardDuty, KMS
    ├── azure/  VNet, AKS, Key Vault, ACR, Log Analytics, Entra workload federation
    └── gcp/    VPC, GKE, Cloud KMS, Artifact Registry, BigQuery, GCS backups
```

## Key design decisions

| Decision | Rationale |
|---|---|
| **Feature flags** `enable_aws` / `enable_azure` / `enable_gcp` with `count` | Clients adopt one cloud at a time. Outputs use `try(module.x[0]…, null)`, so a disabled cloud never breaks the plan. |
| **`name_prefix = "${project}-${environment}"`** | Deterministic and collision-free names (`czarintel-prod-eks`), and IAM conditions can pattern-match on them. |
| **`common_tags`** (Project, Environment, CostCenter, Owner, ManagedBy…) | Cost allocation (CZ-1000) and ownership. AWS applies them through provider `default_tags`, GCP converts them to lowercase labels. |
| **Workspaces per environment** (`dev`, `stg`, `prod`) plus `workspace_key_prefix = "env"` | One backend, isolated state files, the same code for every environment. |
| **Prod-only safety rails** | `deletion_protection = var.environment == "prod"` on RDS, GKE and BigQuery tables; `force_destroy` is disabled in prod. |
| **`prevent_destroy`** on KMS keys | Destroying a key makes every backup unreadable, so Terraform refuses to. |
| **Typed and validated inputs** | `project` and `environment` have `validation` blocks, so a typo such as `prd` fails at `plan` before any API call. Every other input is strictly typed. |

## Remote state (Zero Trust for state)
State files contain resource attributes (endpoints, ARNs, sometimes generated secrets), so they are treated as sensitive data:
- **S3 bucket:** versioned, SSE-KMS, public access blocked, TLS-only. Created by `scripts/bootstrap.sh state`.
- **DynamoDB lock table** `czarintel-terraform-locks` prevents concurrent applies.
- **Access:** SSO/OIDC-assumed roles only. `encrypt = true` is verified by compliance check **TF-001**.

```bash
./scripts/bootstrap.sh state          # once per account
cd terraform
cp terraform.tfvars.example terraform.tfvars   # edit values
terraform init -backend-config="bucket=czarintel-tfstate-<account-id>" -backend-config="region=us-east-1"
terraform workspace select -or-create prod
terraform plan -out=prod.tfplan && terraform apply prod.tfplan
```

## Outputs contract

| Output | Consumed by |
|---|---|
| `aws_eks_cluster_name` | `bootstrap.sh cluster` (`aws eks update-kubeconfig`) |
| `aws_api_service_irsa_role_arn` | Helm `serviceAccount.annotations` (`eks.amazonaws.com/role-arn`) |
| `aws_jenkins_agent_irsa_role_arn` | `ci-cd/jenkins/jenkins-deployment.yml` `jenkins-agent` ServiceAccount |
| `aws_ecr_repository_url` | Jenkinsfile `ECR_REGISTRY`, Gatekeeper `approved-registries-only` |
| `aws_rds_endpoint`, `aws_rds_master_secret_arn` | api-service configuration, External Secrets |
| `gcp_bigquery_dataset_id`, `gcp_artifact_registry_url` | Data pipelines, Gatekeeper allow list |

## Quality gates (`.github/workflows/terraform-validate.yml`)
1. `terraform fmt -check -recursive`
2. `terraform init -backend=false && terraform validate`
3. `tflint --recursive`
4. **checkov** (framework terraform, SARIF → GitHub code scanning)
5. Repo compliance scan: TF-001 (encrypted backend), TF-002 (no hard-coded credentials), TF-003 (encryption at rest on every storage and database resource)

## Operating guidance
- **Never** run `apply` from a laptop against prod. Use the reviewed plan artefact (`bootstrap.sh infra` requires typing `apply`).
- **Drift detection:** run `terraform plan -detailed-exitcode` on a schedule; exit code 2 means drift and opens a ticket.
- **Upgrades:** bump provider pins in a dedicated PR, review the plan for replacements (`-/+`), and apply to `dev` → `stg` → `prod`.
- **Importing existing resources:** use `import {}` blocks (Terraform ≥ 1.5) so imports are code-reviewed too.
