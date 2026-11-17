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

variable "monthly_amount" {
  description = "Monthly subscription budget in its billing currency, not the Retail Prices API's USD."
  type        = number

  validation {
    condition     = var.monthly_amount > 0
    error_message = "The monthly budget must be greater than zero."
  }
}

variable "start_date" {
  description = "First day of the initial budget month at midnight UTC, for example 2026-11-01T00:00:00Z. Keep this fixed on later applies."
  type        = string
}

data "azurerm_subscription" "current" {}

# Separate state keeps alerts active when the disposable AKS cluster is destroyed.
resource "azurerm_consumption_budget_subscription" "optimizer" {
  name            = "cloud-optimizer-monthly"
  subscription_id = data.azurerm_subscription.current.id
  amount          = var.monthly_amount
  time_grain      = "Monthly"

  time_period {
    start_date = var.start_date
  }

  dynamic "notification" {
    for_each = [20, 40, 60, 80, 100]

    content {
      enabled        = true
      operator       = "GreaterThanOrEqualTo"
      threshold      = notification.value
      threshold_type = "Actual"
      contact_roles  = ["Owner"]
    }
  }
}
