###############################################################################
# CzarIntel Azure Landing Zone module
#
#   Resource Group, VNet (AKS / private endpoints subnets) with NSGs
#   AKS: Entra ID-integrated Azure RBAC, local accounts disabled, OIDC issuer +
#        Workload Identity, Azure CNI Overlay + Calico network policy,
#        system + autoscaling user node pools across 3 zones
#   Key Vault (RBAC model, purge protection, network ACLs), ACR Premium
#   Log Analytics + Monitor diagnostic settings (AKS, Key Vault, ACR)
#   Entra ID app registration federated to a Kubernetes ServiceAccount
###############################################################################

data "azurerm_client_config" "current" {}

locals {
  # Azure resource names with strict charsets (KV, ACR) cannot contain hyphens
  compact_name = substr(replace("${var.name_prefix}", "-", ""), 0, 16)
  aks_name     = "${var.name_prefix}-aks"

  tags = merge(var.tags, {
    Environment = var.environment
    Project     = var.project
    ManagedBy   = "Terraform"
  })
}

resource "random_string" "suffix" {
  length  = 4
  special = false
  upper   = false
}

###############################################################################
# Resource group
###############################################################################
resource "azurerm_resource_group" "main" {
  name     = "rg-${var.name_prefix}"
  location = var.location
  tags     = local.tags
}

###############################################################################
# Networking
###############################################################################
resource "azurerm_virtual_network" "main" {
  name                = "vnet-${var.name_prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = [var.vnet_cidr]
  tags                = local.tags
}

resource "azurerm_subnet" "aks_nodes" {
  name                 = "snet-aks-nodes"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 4, 0)]
  service_endpoints    = ["Microsoft.KeyVault", "Microsoft.ContainerRegistry", "Microsoft.Storage"]
}

resource "azurerm_subnet" "private_endpoints" {
  name                              = "snet-private-endpoints"
  resource_group_name               = azurerm_resource_group.main.name
  virtual_network_name              = azurerm_virtual_network.main.name
  address_prefixes                  = [cidrsubnet(var.vnet_cidr, 8, 32)]
  private_endpoint_network_policies = "Enabled"
}

resource "azurerm_network_security_group" "aks_nodes" {
  name                = "nsg-${var.name_prefix}-aks-nodes"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  security_rule {
    name                       = "DenySSHFromInternet"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "DenyRDPFromInternet"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "3389"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  tags = local.tags
}

resource "azurerm_subnet_network_security_group_association" "aks_nodes" {
  subnet_id                 = azurerm_subnet.aks_nodes.id
  network_security_group_id = azurerm_network_security_group.aks_nodes.id
}

###############################################################################
# Log Analytics
###############################################################################
resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-${var.name_prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  daily_quota_gb      = var.environment == "prod" ? -1 : 5
  tags                = local.tags
}

###############################################################################
# Container Registry
###############################################################################
resource "azurerm_container_registry" "main" {
  name                          = "acr${local.compact_name}${random_string.suffix.result}"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  sku                           = "Premium"
  admin_enabled                 = false
  public_network_access_enabled = true
  zone_redundancy_enabled       = true
  anonymous_pull_enabled        = false
  data_endpoint_enabled         = true

  retention_policy {
    days    = 30
    enabled = true
  }

  trust_policy {
    enabled = true # Docker Content Trust — only signed images
  }

  network_rule_set {
    default_action = length(var.api_authorized_ranges) > 0 ? "Deny" : "Allow"

    ip_rule = [for cidr in var.api_authorized_ranges : {
      action   = "Allow"
      ip_range = cidr
    }]
  }

  tags = local.tags
}

###############################################################################
# AKS
###############################################################################
resource "azurerm_user_assigned_identity" "aks" {
  name                = "id-${local.aks_name}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

# AKS control plane needs to manage the node subnet (load balancers, NICs)
resource "azurerm_role_assignment" "aks_network_contributor" {
  scope                = azurerm_virtual_network.main.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_user_assigned_identity.aks.principal_id
}

resource "azurerm_kubernetes_cluster" "main" {
  name                = local.aks_name
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  dns_prefix          = local.aks_name
  kubernetes_version  = var.kubernetes_version
  node_resource_group = "rg-${var.name_prefix}-aks-nodes"
  sku_tier            = var.environment == "prod" ? "Standard" : "Free"

  automatic_channel_upgrade    = "patch"
  node_os_channel_upgrade      = "NodeImage"
  local_account_disabled       = true
  oidc_issuer_enabled          = true
  workload_identity_enabled    = true
  azure_policy_enabled         = true
  image_cleaner_enabled        = true
  image_cleaner_interval_hours = 48

  api_server_access_profile {
    authorized_ip_ranges = var.api_authorized_ranges
  }

  default_node_pool {
    name                         = "system"
    vm_size                      = var.system_node_vm_size
    node_count                   = var.system_node_count
    vnet_subnet_id               = azurerm_subnet.aks_nodes.id
    zones                        = ["1", "2", "3"]
    os_disk_type                 = "Ephemeral"
    os_disk_size_gb              = 100
    os_sku                       = "AzureLinux"
    only_critical_addons_enabled = true
    max_pods                     = 110
    temporary_name_for_rotation  = "systemtmp"

    upgrade_settings {
      max_surge = "33%"
    }

    node_labels = {
      "czarintel.io/node-pool" = "system"
    }

    tags = local.tags
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aks.id]
  }

  azure_active_directory_role_based_access_control {
    managed                = true
    azure_rbac_enabled     = true
    tenant_id              = data.azurerm_client_config.current.tenant_id
    admin_group_object_ids = var.admin_group_object_ids
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "calico"
    load_balancer_sku   = "standard"
    outbound_type       = "loadBalancer"
    pod_cidr            = "192.168.0.0/16"
    service_cidr        = "172.16.0.0/16"
    dns_service_ip      = "172.16.0.10"
  }

  oms_agent {
    log_analytics_workspace_id      = azurerm_log_analytics_workspace.main.id
    msi_auth_for_monitoring_enabled = true
  }

  key_vault_secrets_provider {
    secret_rotation_enabled  = true
    secret_rotation_interval = "2m"
  }

  microsoft_defender {
    log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  }

  maintenance_window_auto_upgrade {
    frequency   = "Weekly"
    interval    = 1
    duration    = 4
    day_of_week = "Sunday"
    start_time  = "03:00"
    utc_offset  = "-05:00"
  }

  tags = local.tags

  lifecycle {
    ignore_changes = [default_node_pool[0].node_count, kubernetes_version]
  }

  depends_on = [azurerm_role_assignment.aks_network_contributor]
}

resource "azurerm_kubernetes_cluster_node_pool" "user" {
  name                  = "user"
  kubernetes_cluster_id = azurerm_kubernetes_cluster.main.id
  vm_size               = var.user_node_vm_size
  vnet_subnet_id        = azurerm_subnet.aks_nodes.id
  zones                 = ["1", "2", "3"]
  mode                  = "User"
  os_sku                = "AzureLinux"
  os_disk_type          = "Ephemeral"
  os_disk_size_gb       = 100
  enable_auto_scaling   = true
  min_count             = var.user_node_min_count
  max_count             = var.user_node_max_count
  max_pods              = 110

  upgrade_settings {
    max_surge = "33%"
  }

  node_labels = {
    "czarintel.io/node-pool" = "user"
    "czarintel.io/env"       = var.environment
  }

  tags = local.tags

  lifecycle {
    ignore_changes = [node_count]
  }
}

# Allow kubelet identity to pull from ACR without image pull secrets
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                            = azurerm_container_registry.main.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.main.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "aks_admins" {
  for_each             = toset(var.admin_group_object_ids)
  scope                = azurerm_kubernetes_cluster.main.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = each.value
}

###############################################################################
# Key Vault
###############################################################################
resource "azurerm_key_vault" "main" {
  name                            = "kv-${local.compact_name}-${random_string.suffix.result}"
  location                        = azurerm_resource_group.main.location
  resource_group_name             = azurerm_resource_group.main.name
  tenant_id                       = data.azurerm_client_config.current.tenant_id
  sku_name                        = "premium" # HSM-backed keys
  soft_delete_retention_days      = 90
  purge_protection_enabled        = true
  enable_rbac_authorization       = false
  enabled_for_disk_encryption     = true
  enabled_for_deployment          = false
  enabled_for_template_deployment = false
  public_network_access_enabled   = true

  network_acls {
    bypass                     = "AzureServices"
    default_action             = length(var.api_authorized_ranges) > 0 ? "Deny" : "Allow"
    ip_rules                   = var.api_authorized_ranges
    virtual_network_subnet_ids = [azurerm_subnet.aks_nodes.id]
  }

  tags = local.tags
}

# Terraform deployer — full secret/key lifecycle management
resource "azurerm_key_vault_access_policy" "deployer" {
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = data.azurerm_client_config.current.object_id

  key_permissions         = ["Get", "List", "Create", "Delete", "Update", "Recover", "Purge", "Rotate", "GetRotationPolicy", "SetRotationPolicy"]
  secret_permissions      = ["Get", "List", "Set", "Delete", "Recover", "Purge"]
  certificate_permissions = ["Get", "List", "Create", "Delete", "Update", "Import", "Recover", "Purge"]
}

# Human admins via Entra ID groups
resource "azurerm_key_vault_access_policy" "admins" {
  for_each     = toset(var.admin_group_object_ids)
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = each.value

  key_permissions    = ["Get", "List", "Create", "Update", "Rotate"]
  secret_permissions = ["Get", "List", "Set"]
}

# AKS Secrets Store CSI driver identity — read-only
resource "azurerm_key_vault_access_policy" "aks_csi" {
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azurerm_kubernetes_cluster.main.key_vault_secrets_provider[0].secret_identity[0].object_id

  secret_permissions = ["Get", "List"]
}

# Workload identity (api-service pods) — read-only secrets
resource "azurerm_key_vault_access_policy" "workload" {
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azuread_service_principal.workload.object_id

  secret_permissions = ["Get", "List"]
}

###############################################################################
# Entra ID — app registration federated with a Kubernetes ServiceAccount
# (Workload Identity: no client secrets anywhere)
###############################################################################
resource "azuread_application" "workload" {
  display_name     = "${var.name_prefix}-api-service-workload"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azurerm_client_config.current.object_id]

  tags = ["czarintel", "workload-identity", var.environment]
}

resource "azuread_service_principal" "workload" {
  client_id                    = azuread_application.workload.client_id
  app_role_assignment_required = true
  owners                       = [data.azurerm_client_config.current.object_id]
}

resource "azuread_application_federated_identity_credential" "workload" {
  application_id = azuread_application.workload.id
  display_name   = "aks-${var.workload_namespace}-${var.workload_service_account}"
  description    = "Trust tokens issued by AKS for ${var.workload_namespace}/${var.workload_service_account}"
  audiences      = ["api://AzureADTokenExchange"]
  issuer         = azurerm_kubernetes_cluster.main.oidc_issuer_url
  subject        = "system:serviceaccount:${var.workload_namespace}:${var.workload_service_account}"
}

###############################################################################
# Azure Monitor diagnostic settings
###############################################################################
resource "azurerm_monitor_diagnostic_setting" "aks" {
  name                           = "diag-${local.aks_name}"
  target_resource_id             = azurerm_kubernetes_cluster.main.id
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.main.id
  log_analytics_destination_type = "Dedicated"

  enabled_log {
    category = "kube-apiserver"
  }
  enabled_log {
    category = "kube-audit-admin"
  }
  enabled_log {
    category = "kube-controller-manager"
  }
  enabled_log {
    category = "kube-scheduler"
  }
  enabled_log {
    category = "cluster-autoscaler"
  }
  enabled_log {
    category = "guard"
  }

  metric {
    category = "AllMetrics"
  }
}

resource "azurerm_monitor_diagnostic_setting" "key_vault" {
  name                       = "diag-${azurerm_key_vault.main.name}"
  target_resource_id         = azurerm_key_vault.main.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  enabled_log {
    category_group = "audit"
  }

  metric {
    category = "AllMetrics"
  }
}

resource "azurerm_monitor_diagnostic_setting" "acr" {
  name                       = "diag-${azurerm_container_registry.main.name}"
  target_resource_id         = azurerm_container_registry.main.id
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id

  enabled_log {
    category = "ContainerRegistryRepositoryEvents"
  }
  enabled_log {
    category = "ContainerRegistryLoginEvents"
  }

  metric {
    category = "AllMetrics"
  }
}
