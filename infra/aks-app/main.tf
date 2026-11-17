terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
  }
}

variable "kubeconfig_path" {
  type = string
}

variable "kube_context" {
  type    = string
  default = "optimizer-m4"
}

variable "workload_image" {
  type = string
}

variable "optimizer_image" {
  type = string
}

variable "workload_allocations" {
  description = "Live allocations supplied by aks-update, not a replacement for the reset baseline."
  type = map(object({
    replicas = number
    requests = map(string)
    limits   = map(string)
  }))
  default = {}
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
  config_context = var.kube_context
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}

resource "kubernetes_namespace_v1" "monitoring" {
  metadata {
    name = "monitoring"
  }
}

resource "kubernetes_namespace_v1" "workloads" {
  metadata {
    name = "workloads"
  }
}

resource "kubernetes_namespace_v1" "loadgen" {
  metadata {
    name = "loadgen"
  }
}

resource "helm_release" "prometheus" {
  name       = "prometheus"
  namespace  = kubernetes_namespace_v1.monitoring.metadata[0].name
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = "91.8.1"
  timeout    = 600
  values = [yamlencode({
    prometheus = {
      prometheusSpec = {
        retention = "1d"
        resources = {
          requests = { cpu = "50m", memory = "512Mi" }
          limits   = { cpu = "500m", memory = "2Gi" }
        }
        storageSpec = {
          volumeClaimTemplate = {
            spec = {
              storageClassName = "managed-csi"
              accessModes      = ["ReadWriteOnce"]
              resources        = { requests = { storage = "4Gi" } }
            }
          }
        }
      }
    }
  })]
}

module "demo" {
  source               = "../modules/demo"
  image                = var.workload_image
  image_pull_policy    = "IfNotPresent"
  workload_namespace   = kubernetes_namespace_v1.workloads.metadata[0].name
  loadgen_namespace    = kubernetes_namespace_v1.loadgen.metadata[0].name
  workload_allocations = var.workload_allocations
}

module "optimizer" {
  source                  = "../modules/optimizer"
  image                   = var.optimizer_image
  storage_class           = "managed-csi"
  cost_mode               = "azure"
  recommendation_provider = var.recommendation_provider
  openai_api_key          = var.openai_api_key
  openai_model            = var.openai_model
  depends_on              = [helm_release.prometheus, module.demo]
}
