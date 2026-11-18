IMAGE_TAG ?= v1
OPTIMIZER_IMAGE ?= cloud-optimizer:v1
AZURE_LOCATION ?= spaincentral
AZURE_NAME ?= cloud-optimizer
AKS_KUBECONFIG ?= $(HOME)/.kube/cloud-optimizer.config
AKS_CONTEXT ?= cloud-optimizer
AKS_DASHBOARD_PORT ?= 8001
LOCAL_TF = terraform -chdir=infra/local
AZURE_TF = terraform -chdir=infra/azure
OPTIMIZER_TF = terraform -chdir=infra/optimizer-local
AKS_KUBECTL = kubectl --kubeconfig="$(AKS_KUBECONFIG)" --context="$(AKS_CONTEXT)"

.PHONY: setup sync minikube infra-init adopt-monitoring build plan deploy prometheus observe dashboard test record smoke-openai azure-init azure-quota azure-plan azure-apply azure-destroy optimizer-init optimizer-build optimizer-plan optimizer-deploy optimizer-dashboard aks-dashboard aks-update

setup:
	$(MAKE) sync
	$(MAKE) minikube
	$(MAKE) infra-init
	$(MAKE) build
	$(MAKE) deploy

sync:
	uv sync --extra dev

minikube:
	minikube start --container-runtime=containerd

infra-init:
	$(LOCAL_TF) init

# Run once when adopting the existing baseline monitoring installation.
adopt-monitoring: infra-init
	$(LOCAL_TF) import kubernetes_namespace_v1.monitoring monitoring
	$(LOCAL_TF) import helm_release.prometheus monitoring/prometheus

build:
	minikube image build -t dummy-app:$(IMAGE_TAG) src/dummy-app/

plan:
	$(LOCAL_TF) plan -var='workload_image=dummy-app:$(IMAGE_TAG)'

deploy:
	$(LOCAL_TF) apply -var='workload_image=dummy-app:$(IMAGE_TAG)'

prometheus:
	kubectl --context=minikube port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090

observe:
	uv run uvicorn optimizer.api:app --host 127.0.0.1 --port 8000

dashboard: observe

test:
	uv run pytest -q

record:
	uv run python scripts/record_observation.py tests/fixtures/prometheus

smoke-openai:
	uv run python scripts/smoke_openai.py

azure-init:
	$(AZURE_TF) init

azure-quota:
	az vm list-usage --location $(AZURE_LOCATION) --output table

azure-plan:
	$(AZURE_TF) plan -var='location=$(AZURE_LOCATION)' -var='name=$(AZURE_NAME)'

azure-apply:
	$(AZURE_TF) apply -var='location=$(AZURE_LOCATION)' -var='name=$(AZURE_NAME)'

azure-destroy:
	$(AZURE_TF) destroy -var='location=$(AZURE_LOCATION)' -var='name=$(AZURE_NAME)'

optimizer-init:
	$(OPTIMIZER_TF) init

optimizer-build:
	minikube image build -t $(OPTIMIZER_IMAGE) -f Dockerfile.optimizer .

optimizer-plan:
	$(OPTIMIZER_TF) plan -var='optimizer_image=$(OPTIMIZER_IMAGE)'

optimizer-deploy:
	$(OPTIMIZER_TF) apply -var='optimizer_image=$(OPTIMIZER_IMAGE)'

optimizer-dashboard:
	kubectl --context=minikube -n optimizer port-forward svc/optimizer 8000:8000

aks-update:
	uv run python scripts/update_aks.py --kubeconfig="$(AKS_KUBECONFIG)" --context="$(AKS_CONTEXT)"

# Avoid port-forward resets; cache deployed assets at the HTTP proxy's rewritten URLs.
aks-dashboard:
	@set -eu; \
	umask 077; \
	assets="$$(mktemp -d)"; \
	trap 'rm -f "$$assets/app.js" "$$assets/styles.css"; rmdir "$$assets"' EXIT; \
	trap 'exit 130' INT; \
	trap 'exit 143' TERM; \
	service_path=/api/v1/namespaces/optimizer/services/http:optimizer:8000/proxy; \
	for file in app.js styles.css; do \
		$(AKS_KUBECTL) get --raw "$$service_path/static/$$file" > "$$assets/$$file"; \
	done; \
	server="$$($(AKS_KUBECTL) config view --minify -o jsonpath='{.clusters[0].cluster.server}')"; \
	$(AKS_KUBECTL) --server="$${server%/}$$service_path" proxy \
		--append-server-path=true --port="$(AKS_DASHBOARD_PORT)" --address=127.0.0.1 \
		--www="$$assets" --www-prefix="$$service_path/static/"
