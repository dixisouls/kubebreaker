#!/usr/bin/env bash
# Refuse to continue unless the kubebreaker kind context exists and its API server answers.
# Make targets always pass --context explicitly, so your current kubectl context is never used.
set -euo pipefail

expected="${KUBE_CONTEXT:-kind-kubebreaker}"

if ! kubectl config get-contexts -o name | grep -qx "$expected"; then
  echo "error: kube context '$expected' not found. Run 'make cluster-up' first." >&2
  exit 1
fi

if ! kubectl --context "$expected" get --raw /readyz >/dev/null 2>&1; then
  echo "error: cluster for context '$expected' is not reachable. Is Docker running?" >&2
  exit 1
fi
