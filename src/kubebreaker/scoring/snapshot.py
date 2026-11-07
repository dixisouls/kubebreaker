"""Snapshots of the cluster state we own, flattened to {object: {path: value}} for diffing.

Only fields we control are captured (spec, labels, data), never status, resourceVersion,
timestamps or fields the API server fills in, so a snapshot of an untouched cluster is
stable from one minute to the next.
"""

import hashlib
import json

from kubernetes import client

from kubebreaker.kube import Kube

Snapshot = dict[str, dict[str, object]]

# Pod template annotations that change on every `rollout restart`; the restart itself is
# tracked as an action, not as a state change
IGNORED_ANNOTATIONS = {"kubectl.kubernetes.io/restartedAt"}
# Service spec fields assigned by the cluster
IGNORED_SERVICE_FIELDS = {"clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy"}


def flatten(value, prefix: str = "") -> dict[str, object]:
    """Flatten nested dicts. Lists of named objects (containers, env vars, ports) are keyed by
    name, so reordering or editing one item doesn't shift the paths of the others."""
    out: dict[str, object] = {}
    if isinstance(value, dict):
        # An empty map contributes nothing: Kubernetes treats {} and a missing field alike
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
        named = all("name" in i for i in value)
        for idx, item in enumerate(value):
            key = item["name"] if named else idx
            out.update(flatten(item, f"{prefix}[{key}]"))
    else:
        out[prefix] = value
    return out


def _serialize(obj) -> dict:
    return client.ApiClient().sanitize_for_serialization(obj)


def _labels(d: dict) -> dict:
    return {"metadata": {"labels": d.get("metadata", {}).get("labels") or {}}}


def deployment_state(d: dict) -> dict:
    spec = json.loads(json.dumps(d.get("spec", {})))
    annotations = spec.get("template", {}).get("metadata", {}).get("annotations") or {}
    for a in IGNORED_ANNOTATIONS:
        annotations.pop(a, None)
    return {**_labels(d), "spec": spec}


def service_state(d: dict) -> dict:
    spec = {k: v for k, v in d.get("spec", {}).items() if k not in IGNORED_SERVICE_FIELDS}
    return {**_labels(d), "spec": spec}


def configmap_state(d: dict) -> dict:
    return {**_labels(d), "data": d.get("data") or {}}


def secret_state(d: dict) -> dict:
    # Hash values: we need to know if they changed, never what they are
    data = {
        k: hashlib.sha256(v.encode()).hexdigest()[:12] for k, v in (d.get("data") or {}).items()
    }
    return {**_labels(d), "type": d.get("type"), "data": data}


def networkpolicy_state(d: dict) -> dict:
    return {**_labels(d), "spec": d.get("spec", {})}


def take_snapshot(k: Kube) -> Snapshot:
    ns = k.namespace
    net = client.NetworkingV1Api(k.api_client)
    sources = [
        ("deployment", k.apps.list_namespaced_deployment(ns).items, deployment_state),
        ("service", k.core.list_namespaced_service(ns).items, service_state),
        ("configmap", k.core.list_namespaced_config_map(ns).items, configmap_state),
        ("secret", k.core.list_namespaced_secret(ns).items, secret_state),
        ("networkpolicy", net.list_namespaced_network_policy(ns).items, networkpolicy_state),
    ]
    snap: Snapshot = {}
    for kind, items, state in sources:
        for item in items:
            d = _serialize(item)
            name = d["metadata"]["name"]
            if kind == "configmap" and name == "kube-root-ca.crt":
                continue  # managed by Kubernetes in every namespace
            snap[f"{kind}/{name}"] = flatten(state(d))
    # CoreDNS lives outside the app namespace but is in scope for DNS faults
    corefile = k.core.read_namespaced_config_map("coredns", "kube-system")
    snap["kube-system/configmap/coredns"] = flatten(configmap_state(_serialize(corefile)))
    return snap
