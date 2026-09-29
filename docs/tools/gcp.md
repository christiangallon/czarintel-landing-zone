# Deep Dive: GCP: Data and ML Platform

**Module:** `terraform/modules/gcp/` · **Region:** `us-central1` (default) · **Subnet:** `10.30.0.0/20` plus secondary ranges for pods and services

## Role in the platform
GCP runs analytics and ML workloads, which are bursty, batch-oriented and data-heavy:
- **BigQuery** is serverless, so there is no cluster to size or patch.
- **GKE** runs feature pipelines and model serving under the strictest admission control in the estate (Binary Authorization).
- **Artifact Registry** holds data and ML images close to the compute.

Only de-identified telemetry flows from production to GCP. PHI and PAN never leave the AWS system of record.

## Enabled APIs
`container`, `artifactregistry`, `containerscanning`, `binaryauthorization`, `cloudkms`, `compute`, `logging`, `monitoring`, `storage`, `bigquery`. They are declared in Terraform, so the project cannot drift.

## GKE hardening

| Setting | Value | Why |
|---|---|---|
| `private_cluster_config` | Private nodes. Private endpoint when `master_authorized_cidrs` is empty. | No public node IPs, minimal API exposure |
| `master_authorized_networks_config` | Admin CIDRs | Allow-listed control plane |
| `datapath_provider` | `ADVANCED_DATAPATH` (Dataplane V2, eBPF) | Built-in NetworkPolicy enforcement and flow logging |
| `workload_identity_config` | `<project>.svc.id.goog` | KSA → GSA federation, no JSON keys |
| `workload_metadata_config` | `GKE_METADATA` | Pods cannot reach node credentials through the metadata server |
| `database_encryption` | Cloud KMS key `gke-secrets` (90-day rotation) | Application-layer secrets encryption |
| `binary_authorization` | `PROJECT_SINGLETON_POLICY_ENFORCE` | Only attested images are admitted |
| `enable_shielded_nodes` + node `shielded_instance_config` | Secure boot and integrity monitoring | Rootkit and bootkit protection |
| `enable_legacy_abac` | `false` | RBAC only |
| `master_auth.client_certificate_config` | Disabled | No long-lived client certificates |
| `security_posture_config` | `BASIC` posture with `VULNERABILITY_BASIC` scanning | Continuous workload posture |
| `release_channel` | Managed | Automatic control-plane patching |
| `deletion_protection` | `true` in prod | Accident protection |

Node pools run under a dedicated least-privilege service account (`logging.logWriter`, `monitoring.metricWriter`, `monitoring.viewer`, `stackdriver.resourceMetadata.writer`, `artifactregistry.reader`), never the Compute Engine default SA.

## Workload Identity binding
`google_service_account_iam_binding.api_service_workload_identity` is **authoritative**: only `serviceAccount:<project>.svc.id.goog[production/api-service]` can impersonate the api-service GSA. Terraform removes any manually added impersonator on the next apply.

## BigQuery analytics
| Resource | Configuration |
|---|---|
| Dataset `czarintel_<env>_analytics` | CMEK (`bigquery-analytics` key, 90-day rotation), label `data-classification=internal`, default partition expiry `analytics_retention_days` (400 days) |
| Table `api_request_events` | Day-partitioned on `event_ts`, `require_partition_filter = true` (prevents costly full scans), clustered by `service, status_code`, table-level CMEK, `deletion_protection` in prod |
| Schema | `event_ts, service, cloud, method, route (templated), status_code, latency_ms, trace_id`. The schema deliberately stores **no** user identifiers, IPs or request bodies. |
| IAM | api-service GSA: `bigquery.dataEditor` **on the dataset only**. `analytics_viewer_groups`: `bigquery.dataViewer`. |

The BigQuery service agent is granted `cryptoKeyEncrypterDecrypter` on the key before the dataset is created (`depends_on`). This is the correct ordering for CMEK datasets.

## Artifact Registry
A Docker-format repository with vulnerability scanning (`containerscanning` API). GKE nodes get reader access. The path `us-central1-docker.pkg.dev/czarintel-data-prod/` is allow-listed by Gatekeeper.

## Cloud Storage backups
- Uniform bucket-level access, `public_access_prevention = "enforced"`, CMEK, versioning, 7-day soft delete.
- Lifecycle: Standard → Nearline (30 d) → Coldline (90 d) → delete (`backup_retention_days`), and old non-current versions are pruned.

## Egress
Cloud Router plus **Cloud NAT** give private nodes outbound access without public IPs. NAT logging is set to errors only, to control cost.
