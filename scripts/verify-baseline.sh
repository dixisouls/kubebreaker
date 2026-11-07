#!/usr/bin/env bash
# Check that the shop app matches its healthy baseline:
#   1. every expected Deployment has all replicas available
#   2. every Service has ready endpoints
#   3. a request through the frontend returns the orders page
# Retries for up to TIMEOUT seconds (default 90). Exits non-zero if the baseline isn't reached.
set -euo pipefail

CONTEXT="${KUBE_CONTEXT:-kind-kubebreaker}"
NS=shop
TIMEOUT="${TIMEOUT:-90}"
DEPLOYMENTS=(postgres redis orders api frontend loadgen)
SERVICES=(postgres redis orders api frontend)

k() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }

check_once() {
  local problems=()

  for d in "${DEPLOYMENTS[@]}"; do
    local want have
    want=$(k get deploy "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo missing)
    have=$(k get deploy "$d" -o jsonpath='{.status.availableReplicas}' 2>/dev/null || echo 0)
    [[ "$want" == "missing" ]] && { problems+=("deployment/$d missing"); continue; }
    [[ "$want" -gt 0 && "${have:-0}" == "$want" ]] || problems+=("deployment/$d ${have:-0}/$want available")
  done

  for s in "${SERVICES[@]}"; do
    local ready
    ready=$(k get endpointslices -l kubernetes.io/service-name="$s" \
      -o jsonpath='{.items[*].endpoints[?(@.conditions.ready==true)].addresses[0]}' 2>/dev/null || true)
    [[ -n "$ready" ]] || problems+=("service/$s has no ready endpoints")
  done

  if ! kubectl --context "$CONTEXT" get --raw "/api/v1/namespaces/$NS/services/frontend:80/proxy/" 2>/dev/null \
      | grep -q "Recent orders"; then
    problems+=("frontend did not return the orders page")
  fi

  if ((${#problems[@]})); then
    printf '%s\n' "${problems[@]}"
    return 1
  fi
}

deadline=$((SECONDS + TIMEOUT))
while true; do
  if out=$(check_once); then
    echo "baseline OK"
    exit 0
  fi
  if ((SECONDS >= deadline)); then
    echo "baseline NOT reached after ${TIMEOUT}s:" >&2
    echo "$out" | sed 's/^/  - /' >&2
    exit 1
  fi
  sleep 3
done
