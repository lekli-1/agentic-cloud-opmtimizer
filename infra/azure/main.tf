terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
}

variable "location" {
  type    = string
  default = "spaincentral"
}

variable "name" {
  type    = string
  default = "cloud-optimizer"
}

variable "node_vm_size" {
  type    = string
  default = "Standard_D2as_v4"
}

# Keep the cluster lifecycle separate from local and application state.
resource "azurerm_resource_group" "smoke" {
  name     = var.name
  location = var.location
}

resource "azurerm_kubernetes_cluster" "smoke" {
  name                = var.name
  location            = azurerm_resource_group.smoke.location
  resource_group_name = azurerm_resource_group.smoke.name
  dns_prefix          = var.name
  # Free applies to the control plane, not the node, disk or networking.
  sku_tier = "Free"

  default_node_pool {
    name            = "system"
    node_count      = 1
    vm_size         = var.node_vm_size
    os_disk_size_gb = 30

    # Match the defaults Azure returns, avoiding a perpetual removal plan.
    upgrade_settings {
      max_surge                     = "10%"
      drain_timeout_in_minutes      = 0
      node_soak_duration_in_minutes = 0
    }
  }
  identity {
    type = "SystemAssigned"
  }
}

output "resource_group" {
  value = azurerm_resource_group.smoke.name
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.smoke.name
}
