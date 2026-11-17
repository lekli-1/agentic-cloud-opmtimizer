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
  type = string
}

variable "workload_namespace" {
  type = string
}

variable "loadgen_namespace" {
  type = string
}

variable "workload_allocations" {
  description = "Optional live allocations for image-only updates; empty uses the demo baseline."
  type = map(object({
    replicas = number
    requests = map(string)
    limits   = map(string)
  }))
  default = {}

  validation {
    condition = length(setsubtract(
      keys(var.workload_allocations), ["web-api", "cpu-hungry", "idle-app", "mem-cache"],
    )) == 0
    error_message = "Allocation overrides must target existing demo workloads."
  }
}

locals {
  # Keep the same scenario baseline on Minikube and AKS.
  workloads = {
    web-api = {
      cpu_request    = "250m", cpu_limit = "500m"
      memory_request = "96Mi", memory_limit = "192Mi", baseline_mb = "0"
    }
    cpu-hungry = {
      cpu_request    = "100m", cpu_limit = "100m"
      memory_request = "64Mi", memory_limit = "128Mi", baseline_mb = "0"
    }
    idle-app = {
      cpu_request    = "250m", cpu_limit = "1000m"
      memory_request = "256Mi", memory_limit = "512Mi", baseline_mb = "0"
    }
    mem-cache = {
      cpu_request    = "100m", cpu_limit = "200m"
      memory_request = "128Mi", memory_limit = "160Mi", baseline_mb = "105"
    }
  }
  traffic = {
    web-api    = { path = "/cpu?ms=40", pause = "0.2" }
    cpu-hungry = { path = "/cpu?ms=200", pause = "0.01" }
    mem-cache  = { path = "/mem?mb=4", pause = "2" }
  }
}

resource "kubernetes_deployment_v1" "workload" {
  for_each = local.workloads
  metadata {
    name      = each.key
    namespace = var.workload_namespace
    labels    = { app = each.key }
  }
  spec {
    replicas = try(var.workload_allocations[each.key].replicas, 1)
    selector {
      match_labels = { app = each.key }
    }
    template {
      metadata {
        labels = { app = each.key }
      }
      spec {
        container {
          name              = "app"
          image             = var.image
          image_pull_policy = var.image_pull_policy
          port {
            container_port = 8080
          }
          env {
            name  = "BASELINE_MEM_MB"
            value = each.value.baseline_mb
          }
          resources {
            requests = try(var.workload_allocations[each.key].requests, {
              cpu = each.value.cpu_request, memory = each.value.memory_request,
            })
            limits = try(var.workload_allocations[each.key].limits, {
              cpu = each.value.cpu_limit, memory = each.value.memory_limit,
            })
          }
          readiness_probe {
            http_get {
              path = "/health"
              port = 8080
            }
            initial_delay_seconds = 3
            period_seconds        = 5
            timeout_seconds       = 2
          }
          liveness_probe {
            http_get {
              path = "/health"
              port = 8080
            }
            initial_delay_seconds = 10
            period_seconds        = 10
            timeout_seconds       = 2
          }
          security_context {
            run_as_non_root            = true
            allow_privilege_escalation = false
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "workload" {
  for_each = local.workloads
  metadata {
    name      = each.key
    namespace = var.workload_namespace
  }
  spec {
    selector = { app = each.key }
    port {
      port        = 80
      target_port = 8080
    }
  }
}

resource "kubernetes_deployment_v1" "loadgen" {
  metadata {
    name      = "loadgen"
    namespace = var.loadgen_namespace
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "loadgen" }
    }
    template {
      metadata {
        labels = { app = "loadgen" }
      }
      spec {
        # Independent loops prevent a slow workload from delaying other traffic.
        dynamic "container" {
          for_each = local.traffic
          content {
            name  = container.key
            image = "curlimages/curl:8.12.1"
            command = ["/bin/sh", "-c", <<-SH
              while true; do
                curl --fail --silent --show-error --output /dev/null --connect-timeout 2 --max-time 15 \
                  'http://${container.key}.${var.workload_namespace}.svc.cluster.local${container.value.path}' \
                  || echo 'Request to ${container.key} failed' >&2
                sleep ${container.value.pause}
              done
            SH
            ]
            resources {
              requests = { cpu = "10m", memory = "16Mi" }
              limits   = { cpu = "100m", memory = "32Mi" }
            }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_service_v1.workload, kubernetes_deployment_v1.workload]
}
