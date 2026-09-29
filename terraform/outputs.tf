###############################################################################
# Root outputs — consumed by scripts/bootstrap.sh and the CI pipeline
###############################################################################

# ----------------------------- AWS -----------------------------------------
output "aws_vpc_id" {
  description = "AWS VPC ID."
  value       = try(module.aws[0].vpc_id, null)
}

output "aws_eks_cluster_name" {
  description = "EKS cluster name (use with `aws eks update-kubeconfig`)."
  value       = try(module.aws[0].eks_cluster_name, null)
}

output "aws_eks_cluster_endpoint" {
  description = "EKS API server endpoint."
  value       = try(module.aws[0].eks_cluster_endpoint, null)
}

output "aws_rds_endpoint" {
  description = "RDS MySQL writer endpoint."
  value       = try(module.aws[0].rds_endpoint, null)
}

output "aws_rds_master_secret_arn" {
  description = "Secrets Manager ARN holding the RDS master credentials (managed by RDS)."
  value       = try(module.aws[0].rds_master_secret_arn, null)
}

output "aws_backup_bucket" {
  description = "S3 bucket for backups and artifacts."
  value       = try(module.aws[0].backup_bucket_name, null)
}

output "aws_api_service_irsa_role_arn" {
  description = "IAM role ARN to annotate on the api-service Kubernetes ServiceAccount."
  value       = try(module.aws[0].api_service_irsa_role_arn, null)
}

output "aws_ecr_repository_url" {
  description = "ECR repository URL for the api-service image."
  value       = try(module.aws[0].ecr_repository_url, null)
}

# ----------------------------- Azure ---------------------------------------
output "azure_resource_group" {
  value = try(module.azure[0].resource_group_name, null)
}

output "azure_aks_cluster_name" {
  value = try(module.azure[0].aks_cluster_name, null)
}

output "azure_key_vault_uri" {
  value = try(module.azure[0].key_vault_uri, null)
}

output "azure_acr_login_server" {
  value = try(module.azure[0].acr_login_server, null)
}

output "azure_workload_identity_client_id" {
  value = try(module.azure[0].workload_identity_client_id, null)
}

# ----------------------------- GCP -----------------------------------------
output "gcp_gke_cluster_name" {
  value = try(module.gcp[0].gke_cluster_name, null)
}

output "gcp_artifact_registry_url" {
  value = try(module.gcp[0].artifact_registry_url, null)
}

output "gcp_backup_bucket" {
  value = try(module.gcp[0].backup_bucket_name, null)
}

# ----------------------------- Convenience ---------------------------------
output "kubeconfig_commands" {
  description = "Commands to configure kubectl contexts for every enabled cluster."
  value = compact([
    var.enable_aws ? "aws eks update-kubeconfig --region ${var.aws_region} --name ${module.aws[0].eks_cluster_name} --alias eks-${var.environment}" : "",
    var.enable_azure ? "az aks get-credentials --resource-group ${module.azure[0].resource_group_name} --name ${module.azure[0].aks_cluster_name} --context aks-${var.environment}" : "",
    var.enable_gcp ? "gcloud container clusters get-credentials ${module.gcp[0].gke_cluster_name} --region ${var.gcp_region} --project ${var.gcp_project_id}" : "",
  ])
}
