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
  type    = string
  default = "~/.kube/config"
}

variable "workload_image" {
  type    = string
  default = "dummy-app:m1-v2"
}

provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = "minikube"
}

provider "helm" {
  kubernetes = {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = "minikube"
  }
}

# Imported, shared monitoring must not be destroyed with the demo workloads.
resource "kubernetes_namespace_v1" "monitoring" {
  metadata {
    name   = "monitoring"
    labels = { name = "monitoring" }
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "helm_release" "prometheus" {
  name       = "prometheus"
  namespace  = kubernetes_namespace_v1.monitoring.metadata[0].name
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = "91.8.1"
  lifecycle {
    prevent_destroy = true
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

module "demo" {
  source             = "../modules/demo"
  image              = var.workload_image
  image_pull_policy  = "Never"
  workload_namespace = kubernetes_namespace_v1.workloads.metadata[0].name
  loadgen_namespace  = kubernetes_namespace_v1.loadgen.metadata[0].name
}

# Preserve existing ownership while sharing the exact baseline with AKS.
moved {
  from = kubernetes_deployment_v1.workload
  to   = module.demo.kubernetes_deployment_v1.workload
}

moved {
  from = kubernetes_service_v1.workload
  to   = module.demo.kubernetes_service_v1.workload
}

moved {
  from = kubernetes_deployment_v1.loadgen
  to   = module.demo.kubernetes_deployment_v1.loadgen
}
