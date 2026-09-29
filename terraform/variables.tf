###############################################################################
# Root module input variables
###############################################################################

# ---------------------------------------------------------------------------
# Global
# ---------------------------------------------------------------------------
variable "project" {
  description = "Project short name used as a prefix for every resource."
  type        = string
  default     = "czarintel"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,15}$", var.project))
    error_message = "project must be 3-16 chars, lowercase alphanumerics and hyphens, starting with a letter."
  }
}

variable "environment" {
  description = "Deployment environment (dev, stg, prod)."
  type        = string

  validation {
    condition     = contains(["dev", "stg", "prod"], var.environment)
    error_message = "environment must be one of: dev, stg, prod."
  }
}

variable "owner" {
  description = "Owning team or individual, used in resource tags."
  type        = string
  default     = "platform-engineering"
}

variable "cost_center" {
  description = "Cost center tag for FinOps chargeback."
  type        = string
  default     = "CZ-1000"
}

variable "enable_aws" {
  description = "Deploy the AWS landing zone module."
  type        = bool
  default     = true
}

variable "enable_azure" {
  description = "Deploy the Azure landing zone module."
  type        = bool
  default     = true
}

variable "enable_gcp" {
  description = "Deploy the GCP landing zone module."
  type        = bool
  default     = true
}

variable "admin_cidr_blocks" {
  description = "CIDR ranges allowed to reach Kubernetes API endpoints (corporate VPN / bastion egress)."
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------
variable "aws_region" {
  description = "Primary AWS region."
  type        = string
  default     = "us-east-1"
}

variable "aws_vpc_cidr" {
  description = "CIDR block for the AWS VPC."
  type        = string
  default     = "10.10.0.0/16"
}

variable "aws_eks_version" {
  description = "Kubernetes version for EKS."
  type        = string
  default     = "1.30"
}

variable "aws_node_instance_types" {
  description = "Instance types for the EKS managed node group."
  type        = list(string)
  default     = ["t3.medium"]
}

variable "aws_node_desired_size" {
  type    = number
  default = 3
}

variable "aws_node_min_size" {
  type    = number
  default = 2
}

variable "aws_node_max_size" {
  type    = number
  default = 6
}

variable "aws_rds_instance_class" {
  description = "RDS MySQL instance class."
  type        = string
  default     = "db.t3.medium"
}

variable "aws_rds_allocated_storage" {
  type    = number
  default = 50
}

# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------
variable "azure_subscription_id" {
  description = "Azure subscription ID."
  type        = string
  default     = null
}

variable "azure_tenant_id" {
  description = "Azure Entra ID tenant ID."
  type        = string
  default     = null
}

variable "azure_location" {
  description = "Azure region."
  type        = string
  default     = "eastus2"
}

variable "azure_vnet_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "azure_aks_version" {
  type    = string
  default = "1.30"
}

variable "azure_admin_group_object_ids" {
  description = "Entra ID group object IDs granted AKS cluster-admin via Azure RBAC."
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# GCP
# ---------------------------------------------------------------------------
variable "gcp_project_id" {
  description = "GCP project ID."
  type        = string
  default     = null
}

variable "gcp_region" {
  type    = string
  default = "us-central1"
}

variable "gcp_subnet_cidr" {
  type    = string
  default = "10.30.0.0/20"
}

variable "gcp_pods_cidr" {
  type    = string
  default = "10.31.0.0/16"
}

variable "gcp_services_cidr" {
  type    = string
  default = "10.32.0.0/20"
}
