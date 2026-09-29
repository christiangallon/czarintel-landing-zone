###############################################################################
# CzarIntel GCP Landing Zone module
#
#   VPC + subnet with secondary ranges, Cloud Router + NAT
#   GKE Standard (regional, private nodes, Workload Identity, Shielded Nodes,
#   Dataplane V2 network policy, Binary Authorization, release channel)
#   Artifact Registry (Docker, vulnerability scanning, cleanup policies)
#   Cloud Storage backup bucket (versioned, uniform access, retention, CMEK)
#   Least-privilege node SA + Workload Identity binding for api-service
###############################################################################

locals {
  cluster_name = "${var.name_prefix}-gke"
}

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_project_service" "apis" {
  for_each = toset([
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "containerscanning.googleapis.com",
    "binaryauthorization.googleapis.com",
    "cloudkms.googleapis.com",
    "compute.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "storage.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

###############################################################################
# Networking
###############################################################################
resource "google_compute_network" "main" {
  name                    = "${var.name_prefix}-vpc"
  project                 = var.project_id
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.apis]
}

resource "google_compute_subnetwork" "gke" {
  name                     = "${var.name_prefix}-gke-subnet"
  project                  = var.project_id
  region                   = var.region
  network                  = google_compute_network.main.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_router" "main" {
  name    = "${var.name_prefix}-router"
  project = var.project_id
  region  = var.region
  network = google_compute_network.main.id
}

resource "google_compute_router_nat" "main" {
  name                               = "${var.name_prefix}-nat"
  project                            = var.project_id
  router                             = google_compute_router.main.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

###############################################################################
# KMS — CMEK for GKE application-layer secrets and GCS
###############################################################################
resource "google_kms_key_ring" "main" {
  name     = "${var.name_prefix}-keyring"
  project  = var.project_id
  location = var.region

  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "gke_secrets" {
  name            = "gke-secrets"
  key_ring        = google_kms_key_ring.main.id
  rotation_period = "7776000s" # 90 days
  labels          = var.labels

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key" "storage" {
  name            = "gcs-backups"
  key_ring        = google_kms_key_ring.main.id
  rotation_period = "7776000s"
  labels          = var.labels

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key_iam_member" "gke_secrets" {
  crypto_key_id = google_kms_crypto_key.gke_secrets.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:service-${data.google_project.current.number}@container-engine-robot.iam.gserviceaccount.com"
}

data "google_storage_project_service_account" "gcs" {
  project = var.project_id
}

resource "google_kms_crypto_key_iam_member" "storage" {
  crypto_key_id = google_kms_crypto_key.storage.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

###############################################################################
# Service accounts
###############################################################################
# Minimal node SA — never use the Compute default SA (Editor role)
resource "google_service_account" "gke_nodes" {
  account_id   = substr("${var.name_prefix}-gke-nodes", 0, 30)
  project      = var.project_id
  display_name = "GKE node SA for ${local.cluster_name}"
}

resource "google_project_iam_member" "gke_nodes" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
    "roles/artifactregistry.reader",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

# Application GSA mapped to the Kubernetes SA via Workload Identity
resource "google_service_account" "api_service" {
  account_id   = substr("${var.name_prefix}-api-svc", 0, 30)
  project      = var.project_id
  display_name = "api-service workload identity"
}

# Authoritative binding: ONLY the api-service KSA may impersonate the GSA
resource "google_service_account_iam_binding" "api_service_workload_identity" {
  service_account_id = google_service_account.api_service.name
  role               = "roles/iam.workloadIdentityUser"
  members = [
    "serviceAccount:${var.project_id}.svc.id.goog[${var.workload_namespace}/${var.workload_service_account}]",
  ]

  depends_on = [google_container_cluster.main]
}

# Project-level authoritative binding for backup writers (api-service GSA only)
resource "google_project_iam_binding" "workload_identity_storage" {
  project = var.project_id
  role    = "roles/storage.objectUser"
  members = [
    "serviceAccount:${google_service_account.api_service.email}",
  ]

  condition {
    title       = "only-backup-bucket"
    description = "Restrict object access to the landing zone backup bucket"
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.backups.name}\")"
  }
}

###############################################################################
# GKE (Standard, regional, private)
###############################################################################
resource "google_container_cluster" "main" {
  provider = google

  name     = local.cluster_name
  project  = var.project_id
  location = var.region

  network    = google_compute_network.main.id
  subnetwork = google_compute_subnetwork.gke.id

  # Node pools are managed separately for zero-downtime changes
  remove_default_node_pool = true
  initial_node_count       = 1

  deletion_protection = var.environment == "prod"
  datapath_provider   = "ADVANCED_DATAPATH" # Dataplane V2 (eBPF) with NetworkPolicy

  release_channel {
    channel = var.environment == "prod" ? "STABLE" : "REGULAR"
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = length(var.master_authorized_cidrs) == 0
    master_ipv4_cidr_block  = var.master_ipv4_cidr

    master_global_access_config {
      enabled = false
    }
  }

  master_authorized_networks_config {
    dynamic "cidr_blocks" {
      for_each = var.master_authorized_cidrs
      content {
        cidr_block   = cidr_blocks.value
        display_name = "admin-${cidr_blocks.key}"
      }
    }
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  database_encryption {
    state    = "ENCRYPTED"
    key_name = google_kms_crypto_key.gke_secrets.id
  }

  binary_authorization {
    evaluation_mode = "PROJECT_SINGLETON_POLICY_ENFORCE"
  }

  enable_shielded_nodes = true
  enable_legacy_abac    = false

  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  security_posture_config {
    mode               = "BASIC"
    vulnerability_mode = "VULNERABILITY_BASIC"
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS", "APISERVER"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS", "APISERVER", "SCHEDULER", "CONTROLLER_MANAGER"]
    managed_prometheus {
      enabled = true
    }
  }

  addons_config {
    http_load_balancing {
      disabled = false
    }
    horizontal_pod_autoscaling {
      disabled = false
    }
    gce_persistent_disk_csi_driver_config {
      enabled = true
    }
  }

  maintenance_policy {
    recurring_window {
      start_time = "2026-01-04T08:00:00Z"
      end_time   = "2026-01-04T12:00:00Z"
      recurrence = "FREQ=WEEKLY;BYDAY=SU"
    }
  }

  resource_labels = var.labels

  depends_on = [
    google_project_service.apis,
    google_kms_crypto_key_iam_member.gke_secrets,
  ]
}

resource "google_container_node_pool" "general" {
  name     = "general"
  project  = var.project_id
  location = var.region
  cluster  = google_container_cluster.main.name

  initial_node_count = var.node_min_count

  autoscaling {
    min_node_count  = var.node_min_count
    max_node_count  = var.node_max_count
    location_policy = "BALANCED"
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type    = var.node_machine_type
    disk_size_gb    = 100
    disk_type       = "pd-balanced"
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.gke_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }

    labels = merge(var.labels, {
      "czarintel-io-node-pool" = "general"
    })

    tags = ["gke-node", local.cluster_name]
  }

  lifecycle {
    ignore_changes = [initial_node_count]
  }
}

###############################################################################
# Artifact Registry
###############################################################################
resource "google_artifact_registry_repository" "containers" {
  repository_id = "${var.name_prefix}-containers"
  project       = var.project_id
  location      = var.region
  format        = "DOCKER"
  description   = "CzarIntel container images (${var.environment})"
  labels        = var.labels

  docker_config {
    immutable_tags = true
  }

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "delete-untagged-after-14d"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "1209600s"
    }
  }

  cleanup_policies {
    id     = "keep-last-50"
    action = "KEEP"
    most_recent_versions {
      keep_count = 50
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository_iam_member" "nodes_reader" {
  project    = var.project_id
  location   = google_artifact_registry_repository.containers.location
  repository = google_artifact_registry_repository.containers.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.gke_nodes.email}"
}

###############################################################################
# Cloud Storage — backups
###############################################################################
resource "google_storage_bucket" "backups" {
  name                        = "${var.name_prefix}-backups-${data.google_project.current.number}"
  project                     = var.project_id
  location                    = upper(var.region)
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.environment != "prod"
  labels                      = var.labels

  versioning {
    enabled = true
  }

  encryption {
    default_kms_key_name = google_kms_crypto_key.storage.id
  }

  soft_delete_policy {
    retention_duration_seconds = 604800 # 7 days
  }

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  lifecycle_rule {
    condition {
      age = var.backup_retention_days
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      num_newer_versions = 5
      with_state         = "ARCHIVED"
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_kms_crypto_key_iam_member.storage]
}
