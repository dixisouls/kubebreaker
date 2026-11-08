#!/usr/bin/env bash
# Keep cluster DNS at its baseline. The first run saves the current CoreDNS config as the
# baseline (in the kubebreaker namespace, which the agent can't read); every later run puts it
# back if a fault or the agent changed it. Run on a healthy cluster first: make cluster-up does.
set -euo pipefail

CONTEXT="${KUBE_CONTEXT:-kind-kubebreaker}"
k() { kubectl --context "$CONTEXT" "$@"; }
BASELINE=coredns-baseline

k create namespace kubebreaker --dry-run=client -o yaml | k apply -f - >/dev/null

if ! k -n kubebreaker get configmap "$BASELINE" >/dev/null 2>&1; then
  echo "saving CoreDNS baseline..."
  k -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}' \
    | k -n kubebreaker create configmap "$BASELINE" --from-file=Corefile=/dev/stdin >/dev/null
fi

want=$(k -n kubebreaker get configmap "$BASELINE" -o jsonpath='{.data.Corefile}')
have=$(k -n kube-system get configmap coredns -o jsonpath='{.data.Corefile}')
replicas=$(k -n kube-system get deployment coredns -o jsonpath='{.spec.replicas}')

if [[ "$want" != "$have" ]]; then
  echo "restoring CoreDNS config..."
  printf '%s\n' "$want" \
    | k -n kube-system create configmap coredns --from-file=Corefile=/dev/stdin \
        --dry-run=client -o yaml \
    | k apply -f - >/dev/null 2>&1
  k -n kube-system rollout restart deployment/coredns >/dev/null
elif [[ "$replicas" != "2" ]]; then
  echo "restoring CoreDNS replicas..."
fi
k -n kube-system scale deployment/coredns --replicas=2 >/dev/null
k -n kube-system rollout status deployment/coredns --timeout=120s >/dev/null
