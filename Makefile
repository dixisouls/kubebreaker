CLUSTER        := kubebreaker
CONTEXT        := kind-$(CLUSTER)
CALICO_VERSION := v3.33.0
CALICO_URL     := https://raw.githubusercontent.com/projectcalico/calico/$(CALICO_VERSION)/manifests/calico.yaml
KUBECTL        := kubectl --context $(CONTEXT)
APP_IMAGE      := kubebreaker/shop:0.1.0
HARNESS_IMAGE  := kubebreaker/harness:dev

.PHONY: help cluster-up cluster-down cni monitoring status prometheus app-build app-load app-reset app-verify shop test selftest harness-image harness require-context

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

cluster-up: ## Create the cluster with Calico, monitoring and the shop app
	@if kind get clusters 2>/dev/null | grep -qx $(CLUSTER); then \
		echo "Cluster $(CLUSTER) already exists"; \
	else \
		kind create cluster --config cluster/kind-config.yaml; \
	fi
	@$(MAKE) --no-print-directory cni monitoring app-load app-reset
	@echo "Cluster ready. Context: $(CONTEXT)"

cluster-down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)

cni: require-context ## Install Calico
	$(KUBECTL) apply --server-side -f $(CALICO_URL) >/dev/null
	$(KUBECTL) -n kube-system rollout status daemonset/calico-node --timeout=300s
	$(KUBECTL) wait --for=condition=Ready nodes --all --timeout=180s

monitoring: require-context ## Install Prometheus and kube-state-metrics
	$(KUBECTL) apply -f cluster/monitoring/prometheus.yaml -f cluster/monitoring/kube-state-metrics.yaml
	$(KUBECTL) -n monitoring rollout status deployment/kube-state-metrics --timeout=180s
	$(KUBECTL) -n monitoring rollout status deployment/prometheus --timeout=180s

status: require-context ## Show nodes and pods
	$(KUBECTL) get nodes -o wide
	$(KUBECTL) get pods -A

prometheus: require-context ## Port-forward Prometheus to localhost:9090
	$(KUBECTL) -n monitoring port-forward svc/prometheus 9090:9090

app-build: ## Build the shop image
	docker build -t $(APP_IMAGE) app

app-load: app-build ## Build the shop image and load it into the cluster
	kind load docker-image $(APP_IMAGE) --name $(CLUSTER)

app-reset: require-context ## Reset the shop app to its baseline and verify it
	scripts/reset-app.sh

app-verify: require-context ## Check the shop app matches its baseline
	scripts/verify-baseline.sh

shop: require-context ## Port-forward the shop frontend to localhost:8080
	$(KUBECTL) -n shop port-forward svc/frontend 8080:80

test: ## Run unit tests (no cluster needed)
	uv run pytest -m "not cluster"

selftest: require-context ## Inject every fault, apply its reference fix, verify recovery
	uv run kubebreaker selftest

harness-image: ## Build the harness container image
	docker build -t $(HARNESS_IMAGE) .

harness: harness-image ## Run the CLI in a container, e.g. make harness ARGS="run wrong-db-host"
	@mkdir -p runs
	@kind get kubeconfig --internal --name $(CLUSTER) > runs/.kubeconfig-internal
	docker run --rm -i --network kind --user $$(id -u):$$(id -g) --env-file .env \
		-v "$(CURDIR)/runs/.kubeconfig-internal:/kube/config:ro" \
		-v "$(CURDIR)/runs:/kubebreaker/runs" \
		$(HARNESS_IMAGE) $(ARGS)

require-context:
	@scripts/require-context.sh
