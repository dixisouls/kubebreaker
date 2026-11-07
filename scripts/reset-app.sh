#!/usr/bin/env bash
# Put the shop app back to its baseline: delete the namespace (which removes any injected
# NetworkPolicies, patched ConfigMaps, deleted Secrets and so on), reapply, verify.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTEXT="${KUBE_CONTEXT:-kind-kubebreaker}"
k() { kubectl --context "$CONTEXT" "$@"; }

"$ROOT/scripts/require-context.sh"

start=$SECONDS
echo "deleting namespace shop..."
# Pods hold no state worth a graceful shutdown, so skip the grace period
k -n shop delete pods --all --grace-period=0 --force --ignore-not-found >/dev/null 2>&1 || true
k delete namespace shop --ignore-not-found --wait=true --timeout=120s >/dev/null

echo "applying baseline manifests..."
k apply -k "$ROOT/app/k8s" >/dev/null

for d in postgres redis orders api frontend loadgen; do
  k -n shop rollout status "deployment/$d" --timeout=120s >/dev/null
done

"$ROOT/scripts/verify-baseline.sh"
echo "reset took $((SECONDS - start))s"
