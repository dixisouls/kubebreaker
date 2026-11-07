CLUSTER        := kubebreaker
CONTEXT        := kind-$(CLUSTER)
CALICO_VERSION := v3.33.0
CALICO_URL     := https://raw.githubusercontent.com/projectcalico/calico/$(CALICO_VERSION)/manifests/calico.yaml
KUBECTL        := kubectl --context $(CONTEXT)

.PHONY: help cluster-up cluster-down cni monitoring status prometheus require-context

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

cluster-up: ## Create the kind cluster with Calico and monitoring
	@if kind get clusters 2>/dev/null | grep -qx $(CLUSTER); then \
		echo "Cluster $(CLUSTER) already exists"; \
	else \
		kind create cluster --config cluster/kind-config.yaml; \
	fi
	@$(MAKE) --no-print-directory cni monitoring
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

require-context:
	@scripts/require-context.sh
