terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.38"
    }
  }
}

variable "image" {
  type = string
}

variable "image_pull_policy" {
  type    = string
  default = "IfNotPresent"
}

variable "storage_class" {
  type = string
}

variable "recommendation_provider" {
  type    = string
  default = "fake"
  validation {
    condition     = contains(["fake", "openai"], var.recommendation_provider)
    error_message = "Use fake or openai."
  }
}

variable "cost_mode" {
  type    = string
  default = "fixed"
  validation {
    condition     = contains(["fixed", "azure"], var.cost_mode)
    error_message = "Use fixed or azure."
  }
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

resource "kubernetes_namespace_v1" "optimizer" {
  metadata {
    name = "optimizer"
  }
}

resource "kubernetes_service_account_v1" "optimizer" {
  metadata {
    name      = "optimizer"
    namespace = kubernetes_namespace_v1.optimizer.metadata[0].name
  }
}

resource "kubernetes_role_v1" "workloads" {
  metadata {
    name      = "optimizer"
    namespace = "workloads"
  }
  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get", "list", "patch"]
  }
}

resource "kubernetes_role_binding_v1" "workloads" {
  metadata {
    name      = "optimizer"
    namespace = "workloads"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.workloads.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.optimizer.metadata[0].name
    namespace = kubernetes_namespace_v1.optimizer.metadata[0].name
  }
}

# Azure prices require cluster-scoped reads, never node writes.
resource "kubernetes_cluster_role_v1" "nodes" {
  count = var.cost_mode == "azure" ? 1 : 0
  metadata {
    name = "optimizer-node-prices"
  }
  rule {
    api_groups = [""]
    resources  = ["nodes"]
    verbs      = ["get", "list"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "nodes" {
  count = var.cost_mode == "azure" ? 1 : 0
  metadata {
    name = "optimizer-node-prices"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.nodes[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.optimizer.metadata[0].name
    namespace = kubernetes_namespace_v1.optimizer.metadata[0].name
  }
}

resource "kubernetes_secret_v1" "openai" {
  count = var.recommendation_provider == "openai" ? 1 : 0
  metadata {
    name      = "optimizer-openai"
    namespace = kubernetes_namespace_v1.optimizer.metadata[0].name
  }
  data = { OPENAI_API_KEY = var.openai_api_key }
}

resource "kubernetes_persistent_volume_claim_v1" "history" {
  metadata {
    name      = "optimizer-history"
    namespace = kubernetes_namespace_v1.optimizer.metadata[0].name
  }
  # AKS CSI storage may wait for a consumer Pod before binding.
  wait_until_bound = false
  spec {
    access_modes       = ["ReadWriteOnce"]
    storage_class_name = var.storage_class
    resources {
      requests = { storage = "4Gi" }
    }
  }
}

resource "kubernetes_deployment_v1" "optimizer" {
  metadata {
    name      = "optimizer"
    namespace = kubernetes_namespace_v1.optimizer.metadata[0].name
  }
  lifecycle {
    precondition {
      condition     = var.recommendation_provider != "openai" || length(trimspace(var.openai_api_key)) > 0
      error_message = "Supply a sensitive OpenAI key before selecting the openai provider."
    }
  }
  spec {
    replicas = 1
    # SQLite and the execution queue must never have overlapping workers.
    strategy {
      type = "Recreate"
    }
    selector {
      match_labels = { app = "optimizer" }
    }
    template {
      metadata {
        labels = { app = "optimizer" }
      }
      spec {
        service_account_name             = kubernetes_service_account_v1.optimizer.metadata[0].name
        termination_grace_period_seconds = 600
        security_context {
          run_as_non_root = true
          run_as_user     = 10001
          run_as_group    = 10001
          fs_group        = 10001
        }
        container {
          name              = "optimizer"
          image             = var.image
          image_pull_policy = var.image_pull_policy
          port {
            container_port = 8000
          }
          env {
            name  = "PROMETHEUS_URL"
            value = "http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090"
          }
          env {
            name  = "DATABASE_PATH"
            value = "/data/optimizer.db"
          }
          env {
            name  = "RECOMMENDATION_PROVIDER"
            value = var.recommendation_provider
          }
          env {
            name  = "COST_MODE"
            value = var.cost_mode
          }
          env {
            name  = "OPENAI_MODEL"
            value = var.openai_model
          }
          dynamic "env" {
            for_each = var.recommendation_provider == "openai" ? [1] : []
            content {
              name = "OPENAI_API_KEY"
              value_from {
                secret_key_ref {
                  name = kubernetes_secret_v1.openai[0].metadata[0].name
                  key  = "OPENAI_API_KEY"
                }
              }
            }
          }
          resources {
            requests = { cpu = "50m", memory = "128Mi" }
            limits   = { cpu = "500m", memory = "512Mi" }
          }
          # Diagnostic /health can fail during expected waits or provider errors.
          # Probe server responsiveness instead, keeping the dashboard accessible.
          readiness_probe {
            http_get {
              path = "/"
              port = 8000
            }
            period_seconds = 5
          }
          liveness_probe {
            http_get {
              path = "/"
              port = 8000
            }
            initial_delay_seconds = 20
            period_seconds        = 10
          }
          security_context {
            allow_privilege_escalation = false
            read_only_root_filesystem  = true
            capabilities {
              drop = ["ALL"]
            }
          }
          volume_mount {
            name       = "history"
            mount_path = "/data"
          }
          volume_mount {
            name       = "tmp"
            mount_path = "/tmp"
          }
        }
        volume {
          name = "history"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim_v1.history.metadata[0].name
          }
        }
        volume {
          name = "tmp"
          empty_dir {}
        }
      }
    }
  }
  depends_on = [
    kubernetes_role_binding_v1.workloads,
    kubernetes_cluster_role_binding_v1.nodes,
  ]
}

resource "kubernetes_service_v1" "optimizer" {
  metadata {
    name      = "optimizer"
    namespace = kubernetes_namespace_v1.optimizer.metadata[0].name
  }
  spec {
    type     = "ClusterIP"
    selector = { app = "optimizer" }
    port {
      port        = 8000
      target_port = 8000
    }
  }
}
