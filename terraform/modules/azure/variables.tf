variable "name_prefix" {
  type = string
}

variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "location" {
  type    = string
  default = "eastus2"
}

variable "vnet_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "kubernetes_version" {
  type    = string
  default = "1.30"
}

variable "system_node_vm_size" {
  type    = string
  default = "Standard_D4s_v5"
}

variable "system_node_count" {
  type    = number
  default = 3
}

variable "user_node_vm_size" {
  type    = string
  default = "Standard_D4s_v5"
}

variable "user_node_min_count" {
  type    = number
  default = 2
}

variable "user_node_max_count" {
  type    = number
  default = 8
}

variable "admin_group_object_ids" {
  description = "Entra ID groups granted AKS RBAC Cluster Admin and Key Vault Administrator."
  type        = list(string)
  default     = []
}

variable "api_authorized_ranges" {
  description = "CIDRs allowed to reach the AKS API server."
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  type    = number
  default = 90
}

variable "workload_namespace" {
  description = "Namespace of the ServiceAccount federated with the Entra ID app registration."
  type        = string
  default     = "staging"
}

variable "workload_service_account" {
  type    = string
  default = "api-service"
}
