###############################################################################
# CzarIntel Enterprise Multi-Cloud DevSecOps Landing Zone — Root module
#
#   AWS   → EKS (production workloads), RDS MySQL, GuardDuty, S3
#   Azure → AKS (staging), Key Vault, ACR, Entra ID workload identity
#   GCP   → GKE (data / analytics), Artifact Registry, GCS backups
#
# Each cloud is an independent child module toggled by enable_<cloud> so the
# landing zone can be rolled out incrementally.
###############################################################################

locals {
  name_prefix = "${var.project}-${var.environment}"

  common_tags = {
    Project     = var.project
    Environment = var.environment
    Owner       = var.owner
    CostCenter  = var.cost_center
    ManagedBy   = "Terraform"
    Repository  = "czarintel-landing-zone"
  }
}

# ---------------------------------------------------------------------------
# AWS — primary production landing zone
# ---------------------------------------------------------------------------
module "aws" {
  source = "./modules/aws"
  count  = var.enable_aws ? 1 : 0

  name_prefix         = local.name_prefix
  environment         = var.environment
  project             = var.project
  tags                = local.common_tags
  vpc_cidr            = var.aws_vpc_cidr
  eks_version         = var.aws_eks_version
  node_instance_types = var.aws_node_instance_types
  node_desired_size   = var.aws_node_desired_size
  node_min_size       = var.aws_node_min_size
  node_max_size       = var.aws_node_max_size
  rds_instance_class  = var.aws_rds_instance_class
  rds_storage_gb      = var.aws_rds_allocated_storage
  public_access_cidrs = var.admin_cidr_blocks
}

# ---------------------------------------------------------------------------
# Azure — staging landing zone
# ---------------------------------------------------------------------------
module "azure" {
  source = "./modules/azure"
  count  = var.enable_azure ? 1 : 0

  name_prefix            = local.name_prefix
  environment            = var.environment
  project                = var.project
  tags                   = local.common_tags
  location               = var.azure_location
  vnet_cidr              = var.azure_vnet_cidr
  kubernetes_version     = var.azure_aks_version
  admin_group_object_ids = var.azure_admin_group_object_ids
  api_authorized_ranges  = var.admin_cidr_blocks
}

# ---------------------------------------------------------------------------
# GCP — data & analytics landing zone
# ---------------------------------------------------------------------------
module "gcp" {
  source = "./modules/gcp"
  count  = var.enable_gcp ? 1 : 0

  name_prefix             = local.name_prefix
  environment             = var.environment
  project_id              = var.gcp_project_id
  region                  = var.gcp_region
  labels                  = { for k, v in local.common_tags : lower(k) => lower(replace(v, "/[^a-zA-Z0-9_-]/", "-")) }
  subnet_cidr             = var.gcp_subnet_cidr
  pods_cidr               = var.gcp_pods_cidr
  services_cidr           = var.gcp_services_cidr
  master_authorized_cidrs = var.admin_cidr_blocks
}
