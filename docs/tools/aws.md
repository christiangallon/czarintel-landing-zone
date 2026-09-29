# Deep Dive: AWS: Primary Production Cloud

**Module:** `terraform/modules/aws/` · **Region:** `us-east-1` (default) · **VPC:** `10.10.0.0/16`

## Role in the platform
AWS hosts every customer-facing production workload: the api-service on EKS, the MySQL system of record on RDS, container images in ECR, backups in S3, and threat detection in GuardDuty and CloudTrail.

## Network

```
                    Internet
                       │
                ┌──────▼──────┐
                │     IGW     │
   ┌────────────┴─────────────┴────────────┐  VPC 10.10.0.0/16  (Flow Logs: ALL → CloudWatch/KMS)
   │  public  /20 ×3 AZ   NAT GW ×AZ  ALB/NLB (ingress-nginx)
   │ ─────────────────────────────────────── │
   │  private /20 ×3 AZ                       │
   │    EKS managed node group (IMDSv2, KMS-encrypted gp3, no public IP)
   │    RDS MySQL Multi-AZ (SG: 3306 from node SG only)
   └──────────────────────────────────────────┘
```

- Subnets are computed with `cidrsubnet(var.vpc_cidr, 4, i)` (public) and `i + 8` (private), so the layout is deterministic for any CIDR.
- `aws_default_security_group.default` has **no rules** (CIS AWS 5.4). Nothing can use the default SG by accident.
- Security groups reference each other, never CIDRs: cluster ↔ nodes, nodes → RDS.

## EKS

| Setting | Value | Why |
|---|---|---|
| Control-plane logs | `api, audit, authenticator, controllerManager, scheduler` | Full audit trail (HIPAA 164.312(b)) |
| Secrets encryption | `encryption_config` with the landing-zone KMS CMK | Envelope encryption of etcd secrets |
| Endpoint access | Private always. Public only if `public_access_cidrs` is set. | Minimises the API attack surface |
| Launch template | `http_tokens = "required"`, hop limit 2, encrypted EBS | IMDSv2 session tokens defeat SSRF credential theft. Pods should use IRSA, never the node role. |
| Add-ons | `vpc-cni`, `coredns`, `kube-proxy`, `aws-ebs-csi-driver` (EBS CSI via its own IRSA role) | Managed, version-pinned, patched by AWS |
| OIDC provider | `aws_iam_openid_connect_provider.eks` (thumbprint via the `tls` provider) | Foundation for IRSA |

## IRSA roles (no static keys)

| Role | Trusted subject | Permissions |
|---|---|---|
| `…-api-service-irsa` | `system:serviceaccount:production:api-service` | Read the RDS-managed master secret, KMS decrypt, S3 `backups/api-service/*` only |
| `…-jenkins-agent` | `system:serviceaccount:jenkins:jenkins-agent` | `ecr:GetAuthorizationToken` plus push/pull on the api-service repository only, 1 h sessions |
| `…-ebs-csi` | `kube-system:ebs-csi-controller-sa` | `AmazonEBSCSIDriverPolicy` |

Every trust policy pins **both** `:sub` and `:aud = sts.amazonaws.com`. Other ServiceAccounts in the same cluster therefore cannot assume these roles.

## RDS MySQL
- Multi-AZ with a synchronous standby, `storage_encrypted` with the CMK, `publicly_accessible = false`.
- **`manage_master_user_password`:** RDS generates the master password and rotates it in Secrets Manager. The password never appears in Terraform state or code.
- Parameter group sets `require_secure_transport = ON` (TLS mandatory). IAM database authentication is enabled.
- Performance Insights is encrypted with the CMK. Enhanced monitoring uses a dedicated role.
- `backup_retention_period` and `deletion_protection` (prod) support point-in-time recovery to any second within the retention window.

## ECR
- `image_tag_mutability = "IMMUTABLE"`: a tag can never be re-pointed, so what was scanned is what runs.
- `scan_on_push = true`, KMS encryption, lifecycle policy keeps the last 50 images.

## S3
| Bucket | Purpose | Controls |
|---|---|---|
| `…-backups-<acct>` | Application and DB exports | Versioning, SSE-KMS, public access block, `BucketOwnerEnforced`, TLS-only policy, lifecycle tiering |
| `…-audit-<acct>` | CloudTrail | Same, plus Glacier IR after 90 days and 6-year retention (`audit_log_retention_days = 2190`) |

## Detection
- **CloudTrail** `…-trail`: multi-region, global service events, **log-file validation** (SHA-256 digest chain proves logs were not altered), KMS, S3 data events on the backups bucket, API-call-rate Insights.
- **GuardDuty:** S3 data events, EKS audit logs, EBS malware protection, RDS login events, and Runtime Monitoring with EKS add-on management.
- **Alert routing:** EventBridge rule (severity ≥ 7) → SNS `security-alerts` (KMS-encrypted, topic policy scoped to EventBridge) → PagerDuty/Slack subscriptions.

## KMS
There is one customer-managed key per environment with annual rotation (`enable_key_rotation`) and a 30-day deletion window. Its key policy grants:
- root administration
- CloudWatch Logs, scoped by an encryption-context condition to this account's log groups
- CloudTrail, scoped to this account's trails

## Cost notes (us-east-1, list price, estimate)
| Item | Approx. monthly |
|---|---|
| EKS control plane | ~$73 |
| 3× NAT gateway | ~$100 plus data processing |
| RDS db.t3.medium Multi-AZ | ~$100 |
| GuardDuty / CloudTrail | Usage-based, typically low double digits at this scale |

Run `scripts/cost_report.py --budget <usd>` for actual spend by service and tag.
