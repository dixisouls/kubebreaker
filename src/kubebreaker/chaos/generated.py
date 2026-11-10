"""Faults written as data (by a model or by hand) instead of code.

A spec is a YAML file in faults/generated/. It never contains code: injection and the
reference fix are lists of operations from a short allowlist, checked by `validate` before
anything touches the cluster:

  - patch    a strategic-merge patch to an existing Deployment, Service or ConfigMap of the
             shop app, limited to fields a misconfiguration would plausibly touch
  - create   a new NetworkPolicy in the shop namespace
  - delete   a NetworkPolicy created by this fault (reference fix only)
  - restart  a Deployment (e.g. so a ConfigMap change takes effect)

Never allowed: container commands, exec probes, images other than a different tag of the
shop image, security settings, volumes, Secrets, the load generator, or anything outside the
shop namespace. Container args must be plain flags without spaces or shell characters (no
baseline container runs a shell, so args can't run commands).

The symptom comes from the alert name, the fix scope from the injected patches. A spec joins
experiments (`faults: [generated]`) only once `kubebreaker selftest` has passed on it, which
sets `verified: true` in the file.
"""

import re
from pathlib import Path

import yaml
from kubernetes import client

from kubebreaker.chaos.base import CATALOG, Fault, Tier
from kubebreaker.chaos.checks import deployment_problems, frontend_problems, pod_waiting_reasons
from kubebreaker.chaos.faults_hard import _latency_high, _latency_problems
from kubebreaker.kube import Kube
from kubebreaker.scoring.snapshot import flatten

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATED_DIR = REPO_ROOT / "faults" / "generated"
APP_DIR = REPO_ROOT / "app" / "k8s"

ALERTS = ("HighErrorRate", "HighLatency", "KubePodCrashLooping", "KubeDeploymentRolloutStuck")
# Alerts that name a deployment need `target`
TARGETED_ALERTS = ("KubePodCrashLooping", "KubeDeploymentRolloutStuck")
TIERS = ("easy", "medium", "hard")
SHOP_IMAGE = "kubebreaker/shop:"
# The load generator is the traffic every symptom is measured with
OFF_LIMITS = {"loadgen"}
SAFE_ARG = re.compile(r"^[A-Za-z0-9_.:/=,+%@-]*$")
ID = re.compile(r"^gen-[a-z0-9]+(-[a-z0-9]+)*$")
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,50}[a-z0-9])?$")

# Allowed patch shapes. A dict maps allowed keys to their shape; "str", "int", "scalar" and
# "labels" (a str -> str map) are leaves; ("list", shape) is a list of that shape.
PROBE = {
    "httpGet": {"path": "str", "port": "scalar"},
    "tcpSocket": {"port": "scalar"},
    "initialDelaySeconds": "int",
    "periodSeconds": "int",
    "timeoutSeconds": "int",
    "failureThreshold": "int",
    "successThreshold": "int",
}
ENV_VALUE_FROM = {
    "configMapKeyRef": {"name": "str", "key": "str", "optional": "scalar"},
    "secretKeyRef": {"name": "str", "key": "str", "optional": "scalar"},
}
CONTAINER = {
    "name": "str",
    "args": ("list", "arg"),
    "env": ("list", {"name": "str", "value": "str", "valueFrom": ENV_VALUE_FROM}),
    "resources": {
        "requests": {"cpu": "scalar", "memory": "scalar"},
        "limits": {"cpu": "scalar", "memory": "scalar"},
    },
    "readinessProbe": PROBE,
    "livenessProbe": PROBE,
    "startupProbe": PROBE,
    "ports": ("list", {"name": "str", "containerPort": "int", "protocol": "str"}),
    "image": "image",
}
SHAPES = {
    "deployment": {
        "spec": {
            "replicas": "int",
            "template": {
                "metadata": {"labels": "labels"},
                "spec": {"containers": ("list", CONTAINER)},
            },
        }
    },
    "service": {
        "spec": {
            "selector": "labels",
            "ports": (
                "list",
                {"name": "str", "port": "int", "targetPort": "scalar", "protocol": "str"},
            ),
        }
    },
    "configmap": {"data": "labels"},
}
POLICY_KEYS = {"podSelector", "policyTypes", "ingress", "egress"}


def baseline_objects() -> dict[str, dict[str, dict]]:
    """{kind: {name: manifest}} for the shop app's baseline manifests."""
    objects: dict[str, dict[str, dict]] = {}
    for f in sorted(APP_DIR.glob("*.yaml")):
        for d in yaml.safe_load_all(f.read_text()):
            if d and d.get("kind") not in (None, "Kustomization", "Namespace"):
                objects.setdefault(d["kind"].lower(), {})[d["metadata"]["name"]] = d
    return objects


def _check(value, shape, path: str, errors: list[str]) -> None:
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected a map")
            return
        for k, v in value.items():
            if k not in shape:
                errors.append(f"{path}.{k}: not an allowed field")
            else:
                _check(v, shape[k], f"{path}.{k}", errors)
    elif isinstance(shape, tuple):
        if not isinstance(value, list):
            errors.append(f"{path}: expected a list")
            return
        for i, item in enumerate(value):
            _check(item, shape[1], f"{path}[{i}]", errors)
    elif shape == "arg":
        if not isinstance(value, str) or not SAFE_ARG.match(value):
            errors.append(f"{path}: args must be plain flags without spaces or shell characters")
    elif shape == "image":
        if not (isinstance(value, str) and value.startswith(SHOP_IMAGE)):
            errors.append(f"{path}: only a different tag of {SHOP_IMAGE.rstrip(':')} is allowed")
    elif shape == "labels":
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str | None) for k, v in value.items()
        ):
            errors.append(f"{path}: expected a map of strings")
    elif shape == "int":
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100_000:
            errors.append(f"{path}: expected a small whole number")
    elif shape == "str":
        if not isinstance(value, str) or len(value) > 200:
            errors.append(f"{path}: expected a short string")
    elif not isinstance(value, str | int | float | bool):
        errors.append(f"{path}: expected a single value")


def _check_policy(spec, path: str, errors: list[str]) -> None:
    if not isinstance(spec, dict) or not spec.keys() <= POLICY_KEYS or "podSelector" not in spec:
        errors.append(f"{path}: a NetworkPolicy spec has only {', '.join(sorted(POLICY_KEYS))}")


def _check_ops(ops, where: str, base: dict, created: set, errors: list[str]) -> None:
    if not isinstance(ops, list) or not ops:
        errors.append(f"{where}: expected a non-empty list of operations")
        return
    for i, op in enumerate(ops):
        path = f"{where}[{i}]"
        if not isinstance(op, dict):
            errors.append(f"{path}: expected a map")
            continue
        action, kind, name = op.get("op"), op.get("kind"), op.get("name")
        if not isinstance(name, str) or not NAME.match(name):
            errors.append(f"{path}.name: expected a Kubernetes object name")
            continue
        if name in OFF_LIMITS:
            errors.append(f"{path}: {name} is off limits")
            continue
        if action == "patch":
            if kind not in SHAPES:
                errors.append(f"{path}: patch works on {', '.join(SHAPES)}")
            elif name not in base.get(kind, {}):
                errors.append(f"{path}: no baseline {kind} named {name}")
            else:
                _check(op.get("patch"), SHAPES[kind], f"{path}.patch", errors)
                _check_containers(op.get("patch"), base[kind][name], path, errors)
        elif action == "create":
            if kind != "networkpolicy":
                errors.append(f"{path}: only NetworkPolicies can be created")
            elif where != "inject":
                errors.append(f"{path}: create belongs in inject")
            else:
                created.add(name)
                _check_policy(op.get("spec"), f"{path}.spec", errors)
        elif action == "delete":
            if kind != "networkpolicy" or name not in created:
                errors.append(f"{path}: only a NetworkPolicy this fault created can be deleted")
        elif action == "restart":
            if kind != "deployment" or name not in base.get("deployment", {}):
                errors.append(f"{path}: restart takes a baseline deployment")
        else:
            errors.append(f"{path}.op: expected patch, create, delete or restart")


def _check_containers(patch, manifest: dict, path: str, errors: list[str]) -> None:
    """Container patches must name a container the Deployment already has."""
    if manifest.get("kind") != "Deployment" or not isinstance(patch, dict):
        return
    have = {c["name"] for c in manifest["spec"]["template"]["spec"]["containers"]}
    containers = patch.get("spec", {}).get("template", {}).get("spec", {}).get("containers")
    for c in containers if isinstance(containers, list) else []:
        if isinstance(c, dict) and c.get("name") not in have:
            errors.append(f"{path}: deployment has no container {c.get('name')!r}")


def fix_scope(spec: dict) -> tuple[str, ...]:
    """Where the fix belongs, from the injected operations, in snapshot path form."""
    scopes = []
    for op in spec["inject"]:
        prefix = f"{op['kind']}/{op['name']}"
        if op["op"] == "create":
            scopes.append(prefix)
        elif op["op"] == "patch":
            for path in flatten(op["patch"]):
                # A container's name only says which container to merge into
                if not re.search(r"containers\[[^\]]+\]\.name$", path):
                    scopes.append(f"{prefix}:{_scope_path(path)}")
    return tuple(dict.fromkeys(scopes))


def _scope_path(path: str) -> str:
    # Inside a container, the whole field (args, env, resources, a probe) is the scope, so a
    # fix may rewrite it differently than the injection did
    m = re.match(r"^(.*containers\[[^\]]+\]\.[A-Za-z]+)", path)
    if m:
        return m.group(1)
    parts = path.split(".")
    return ".".join(parts[:2]) if parts[0] in ("spec", "data") else parts[0]


def validate(spec) -> list[str]:
    """Everything wrong with a spec; empty means it's safe to try on the cluster."""
    if not isinstance(spec, dict):
        return ["spec must be a map"]
    errors: list[str] = []
    for key in ("id", "title", "tier", "alert", "ground_truth", "expected_fix"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            errors.append(f"{key}: required text")
    if errors:
        return errors
    if not ID.match(spec["id"]):
        errors.append("id: must look like gen-some-name")
    if spec["id"] in CATALOG:
        errors.append("id: already in the catalog")
    if spec["tier"] not in TIERS:
        errors.append(f"tier: one of {', '.join(TIERS)}")
    name = spec["alert"].split(":", 1)[0]
    if name not in ALERTS or ":" not in spec["alert"]:
        errors.append(f"alert: must start with one of {', '.join(ALERTS)} and a colon")
    if name in TARGETED_ALERTS:
        target = spec.get("target")
        if target not in baseline_objects().get("deployment", {}) or target in OFF_LIMITS:
            errors.append(f"target: {name} needs the deployment it's about")
        elif f"shop/{target}" not in spec["alert"]:
            errors.append(f"alert: should name the deployment as shop/{target}")
    base = baseline_objects()
    created: set[str] = set()
    _check_ops(spec.get("inject"), "inject", base, created, errors)
    _check_ops(spec.get("reference_fix"), "reference_fix", base, created, errors)
    if errors:
        return errors
    for op in spec["inject"]:
        if op["op"] == "create" and op["name"] in base.get("networkpolicy", {}):
            errors.append(f"inject: networkpolicy {op['name']} already exists")
    # The alert describes the symptom from the outside: no injected values in it
    alert = spec["alert"].lower()
    for op in spec["inject"]:
        for value in flatten(op.get("patch") or op.get("spec") or {}).values():
            v = str(value).lower()
            if len(v) >= 4 and v in alert:
                errors.append(f"alert: gives away the injected value {value!r}")
    scope = set(fix_scope(spec))
    for f in CATALOG.values():
        if scope and scope == set(f.fix_scope):
            errors.append(f"inject: changes the same thing as catalog fault {f.id}")
    return errors


def _apply(k: Kube, op: dict) -> None:
    kind, name, ns = op["kind"], op["name"], k.namespace
    net = client.NetworkingV1Api(k.api_client)
    if op["op"] == "restart":
        k.restart_deployment(name)
    elif op["op"] == "create":
        body = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name},
            "spec": op["spec"],
        }
        net.create_namespaced_network_policy(ns, body)
    elif op["op"] == "delete":
        net.delete_namespaced_network_policy(name, ns)
    elif kind == "deployment":
        k.apps.patch_namespaced_deployment(name, ns, op["patch"])
    elif kind == "service":
        k.core.patch_namespaced_service(name, ns, op["patch"])
    elif kind == "configmap":
        k.core.patch_namespaced_config_map(name, ns, op["patch"])


def _symptom(spec: dict):
    name, target = spec["alert"].split(":", 1)[0], spec.get("target")
    if name == "HighLatency":
        return _latency_high
    if name == "KubePodCrashLooping":
        return lambda k: "CrashLoopBackOff" in pod_waiting_reasons(k, target)
    if name == "KubeDeploymentRolloutStuck":
        return lambda k: bool(deployment_problems(k, target))
    return lambda k: bool(frontend_problems(k))


def _runner(ops: list[dict]):
    def run(k: Kube) -> None:
        for op in ops:
            _apply(k, op)

    return run


def build(spec: dict) -> Fault:
    """A Fault from a validated spec."""
    latency = spec["alert"].startswith("HighLatency")
    return Fault(
        id=spec["id"],
        tier=Tier(spec["tier"]),
        title=spec["title"],
        alert=spec["alert"],
        ground_truth=spec["ground_truth"],
        expected_fix=spec["expected_fix"],
        inject=_runner(spec["inject"]),
        symptom=_symptom(spec),
        reference_fix=_runner(spec["reference_fix"]),
        extra_checks=_latency_problems if latency else (lambda k: []),
        fix_scope=fix_scope(spec),
        allowed_restarts=tuple(o["name"] for o in spec["inject"] if o["op"] == "restart"),
    )


def spec_path(fault_id: str, directory: Path = GENERATED_DIR) -> Path:
    return directory / f"{fault_id}.yaml"


def load_all(directory: Path = GENERATED_DIR) -> dict[str, tuple[dict, Fault]]:
    """Every valid spec on disk, verified or not, as {id: (spec, fault)}."""
    out = {}
    for f in sorted(directory.glob("gen-*.yaml")):
        spec = yaml.safe_load(f.read_text())
        if not validate(spec):
            out[spec["id"]] = (spec, build(spec))
    return out


def mark_verified(fault_id: str, directory: Path = GENERATED_DIR) -> None:
    path = spec_path(fault_id, directory)
    spec = yaml.safe_load(path.read_text())
    spec["verified"] = True
    save(spec, directory)


def save(spec: dict, directory: Path = GENERATED_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = spec_path(spec["id"], directory)
    path.write_text(yaml.safe_dump(spec, sort_keys=False, width=100))
    return path
