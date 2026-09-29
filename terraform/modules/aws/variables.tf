variable "name_prefix" {
  description = "Prefix for all resource names, e.g. czarintel-prod."
  type        = string
}

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "tags" {
  description = "Common tags applied to every resource."
  type        = map(string)
  default     = {}
}

variable "vpc_cidr" {
  description = "VPC CIDR block. Subnets are carved out as /20s."
  type        = string
  default     = "10.10.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to span."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3 for high availability."
  }
}

variable "single_nat_gateway" {
  description = "Use one NAT gateway for all AZs (cheaper, non-HA). Recommended only for dev."
  type        = bool
  default     = false
}

variable "eks_version" {
  type    = string
  default = "1.30"
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint. Empty list disables public access."
  type        = list(string)
  default     = []
}

variable "node_instance_types" {
  type    = list(string)
  default = ["t3.medium"]
}

variable "node_desired_size" {
  type    = number
  default = 3
}

variable "node_min_size" {
  type    = number
  default = 2
}

variable "node_max_size" {
  type    = number
  default = 6
}

variable "node_disk_size" {
  type    = number
  default = 50
}

variable "rds_instance_class" {
  type    = string
  default = "db.t3.medium"
}

variable "rds_storage_gb" {
  type    = number
  default = 50
}

variable "rds_engine_version" {
  type    = string
  default = "8.0"
}

variable "rds_database_name" {
  type    = string
  default = "czarintel"
}

variable "rds_master_username" {
  type    = string
  default = "czadmin"
}

variable "rds_backup_retention_days" {
  type    = number
  default = 14
}

variable "log_retention_days" {
  type    = number
  default = 90
}

variable "api_service_namespace" {
  description = "Kubernetes namespace of the api-service ServiceAccount trusted by IRSA."
  type        = string
  default     = "production"
}

variable "api_service_account" {
  description = "Kubernetes ServiceAccount name trusted by IRSA."
  type        = string
  default     = "api-service"
}
