terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
  }
}

variable "kubeconfig_path" {
  type    = string
  default = "~/.kube/config"
}

variable "optimizer_image" {
  type    = string
  default = "cloud-optimizer:m4-v2"
}

variable "recommendation_provider" {
  type    = string
  default = "fake"
}

variable "openai_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "openai_model" {
  type    = string
  default = "gpt-5.4-mini"
}

provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = "minikube"
}

# Separate state prevents deploying the agent from resetting demo allocations.
module "optimizer" {
  source                  = "../modules/optimizer"
  image                   = var.optimizer_image
  image_pull_policy       = "Never"
  storage_class           = "standard"
  recommendation_provider = var.recommendation_provider
  openai_api_key          = var.openai_api_key
  openai_model            = var.openai_model
}
