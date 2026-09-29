variable "name_prefix" {
  type = string
}

variable "environment" {
  type = string
}

variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "labels" {
  description = "GCP labels (lowercase keys/values)."
  type        = map(string)
  default     = {}
}

variable "subnet_cidr" {
  type    = string
  default = "10.30.0.0/20"
}

variable "pods_cidr" {
  type    = string
  default = "10.31.0.0/16"
}

variable "services_cidr" {
  type    = string
  default = "10.32.0.0/20"
}

variable "master_ipv4_cidr" {
  description = "/28 range for the private GKE control plane."
  type        = string
  default     = "172.16.32.0/28"
}

variable "master_authorized_cidrs" {
  type    = list(string)
  default = []
}

variable "node_machine_type" {
  type    = string
  default = "e2-standard-4"
}

variable "node_min_count" {
  type    = number
  default = 1
}

variable "node_max_count" {
  type    = number
  default = 5
}

variable "workload_namespace" {
  type    = string
  default = "production"
}

variable "workload_service_account" {
  type    = string
  default = "api-service"
}

variable "backup_retention_days" {
  type    = number
  default = 365
}
