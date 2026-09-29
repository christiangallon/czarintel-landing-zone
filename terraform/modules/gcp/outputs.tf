output "network_id" {
  value = google_compute_network.main.id
}

output "gke_cluster_name" {
  value = google_container_cluster.main.name
}

output "gke_cluster_endpoint" {
  value     = google_container_cluster.main.endpoint
  sensitive = true
}

output "gke_workload_pool" {
  value = "${var.project_id}.svc.id.goog"
}

output "artifact_registry_url" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.containers.repository_id}"
}

output "backup_bucket_name" {
  value = google_storage_bucket.backups.name
}

output "api_service_gsa_email" {
  description = "Annotate the KSA with iam.gke.io/gcp-service-account=<this>."
  value       = google_service_account.api_service.email
}
