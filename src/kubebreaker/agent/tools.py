"""The SRE agent's tools. Structured, few, and safe:

- every call runs as the sre-agent ServiceAccount, so RBAC is the final guard
- only allowlisted namespaces, kinds and actions; there is no shell and no generic delete
- every change is dry-run on the server first and the diff is returned and logged
- Secret values are never shown, only names and keys
- output is trimmed so one noisy call can't flood the context
"""

import difflib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from kubebreaker.kube import Kube
from kubebreaker.llm import ToolSpec

NAMESPACES = ["shop", "kube-system"]
LIST_KINDS = [
    "pods",
    "deployments",
    "replicasets",
    "services",
    "endpointslices",
    "configmaps",
    "secrets",
    "networkpolicies",
]
DESCRIBE_KINDS = [
    "pod",
    "deployment",
    "replicaset",
    "service",
    "configmap",
    "secret",
    "networkpolicy",
]
PATCH_KINDS = ["deployment", "service", "configmap", "networkpolicy"]
DELETE_KINDS = ["pod", "networkpolicy"]
MAX_OUTPUT_CHARS = 6000
MAX_LOG_LINES = 300
FINAL_TOOL = "declare_resolved"


class ToolError(Exception):
    """A problem the agent caused and can correct (bad kind, bad namespace, bad patch)."""


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    cut = len(text) - limit
    return f"{text[:half]}\n... [{cut} characters trimmed] ...\n{text[-half:]}"


def age(ts: datetime | None) -> str:
    if not ts:
        return "?"
    s = int((datetime.now(UTC) - ts).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{s // size}{unit}"
    return f"{s}s"


def table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "(none)"
    cells = [headers] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells
    )


def to_dict(obj) -> dict:
    return client.ApiClient().sanitize_for_serialization(obj)


def redact_secret(obj: dict) -> dict:
    """Keep a Secret's shape (names and keys) and drop its values."""
    obj = dict(obj)
    for field in ("data", "stringData"):
        if obj.get(field):
            obj[field] = {k: "<redacted>" for k in obj[field]}
    meta = dict(obj.get("metadata", {}))
    annotations = dict(meta.get("annotations") or {})
    annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    meta["annotations"] = annotations
    obj["metadata"] = meta
    return obj


def clean(obj: dict) -> dict:
    """Drop fields that are noise for diagnosis."""
    meta = obj.get("metadata", {})
    meta.pop("managedFields", None)
    (meta.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return obj


def comparable(kind: str, obj: dict) -> dict:
    """The parts of an object a patch is meant to change, for diffs."""
    meta = obj.get("metadata", {})
    out = {"labels": meta.get("labels") or {}}
    if kind == "configmap":
        out["data"] = obj.get("data") or {}
    else:
        out["spec"] = obj.get("spec") or {}
    return out


def diff(kind: str, before: dict, after: dict) -> str:
    a = yaml.safe_dump(comparable(kind, before), sort_keys=True).splitlines()
    b = yaml.safe_dump(comparable(kind, after), sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(a, b, "before", "after", lineterm="", n=2))


def api_error(exc: ApiException) -> str:
    try:
        message = json.loads(exc.body).get("message", exc.reason)
    except (TypeError, ValueError):
        message = exc.reason
    return f"error: Kubernetes API returned {exc.status}: {message}"


def _str(description: str, enum: list[str] | None = None) -> dict:
    schema = {"type": "string", "description": description}
    if enum:
        schema["enum"] = enum
    return schema


# A short stated purpose per call keeps traces readable. Asking the model to narrate its
# reasoning here instead ("your current hypothesis...") trips Anthropic's
# reasoning_extraction safeguard on Claude Sonnet 5.5.
PURPOSE = _str("One short sentence: what this call checks or changes, and why.")
NAMESPACE = _str("Namespace (default shop).", NAMESPACES)


def _spec(name: str, description: str, props: dict, required: list[str]) -> ToolSpec:
    return ToolSpec(
        name,
        description,
        {
            "type": "object",
            "properties": {"purpose": PURPOSE, **props},
            "required": ["purpose", *required],
            "additionalProperties": False,
        },
    )


SPECS = [
    _spec(
        "get_resources",
        "List resources of one kind in a namespace, as a compact table.",
        {"kind": _str("Resource kind.", LIST_KINDS), "namespace": NAMESPACE},
        ["kind"],
    ),
    _spec(
        "describe",
        "Show one resource in full (YAML, with status). For pods, also shows recent events "
        "for that pod. Secret values are redacted.",
        {
            "kind": _str("Resource kind.", DESCRIBE_KINDS),
            "name": _str("Resource name."),
            "namespace": NAMESPACE,
        },
        ["kind", "name"],
    ),
    _spec(
        "get_logs",
        "Tail a pod's logs.",
        {
            "pod": _str("Pod name."),
            "namespace": NAMESPACE,
            "tail_lines": {
                "type": "integer",
                "description": f"Lines to return (max {MAX_LOG_LINES}).",
            },
            "previous": {
                "type": "boolean",
                "description": "Logs of the previous (crashed) container, not the current one.",
            },
        },
        ["pod"],
    ),
    _spec(
        "get_events",
        "Recent Kubernetes events in a namespace, newest last.",
        {"namespace": NAMESPACE},
        [],
    ),
    _spec(
        "query_metrics",
        "Run an instant PromQL query against Prometheus. Useful metrics: "
        "http_requests_total{service,route,status}, http_request_duration_seconds_bucket, "
        "dependency_request_duration_seconds_bucket{service,dependency,outcome}, "
        "kube-state-metrics (kube_pod_container_status_restarts_total, "
        "kube_pod_container_status_last_terminated_reason, "
        "kube_deployment_status_replicas_available, ...), and cAdvisor "
        "(container_cpu_usage_seconds_total, container_cpu_cfs_throttled_periods_total, "
        "container_memory_working_set_bytes).",
        {"promql": _str("PromQL expression.")},
        ["promql"],
    ),
    _spec(
        "apply_patch",
        "Change a resource with a strategic merge patch (lists of containers merge by name). "
        "The patch is dry-run first; the response shows the diff that was applied.",
        {
            "kind": _str("Resource kind.", PATCH_KINDS),
            "name": _str("Resource name."),
            "namespace": NAMESPACE,
            "patch": {
                "type": "object",
                "description": 'Patch body, e.g. {"spec": {"replicas": 2}}.',
            },
        },
        ["kind", "name", "patch"],
    ),
    _spec(
        "rollout",
        "Deployment rollouts: 'status' shows progress, 'restart' recreates all pods, "
        "'undo' rolls back to the previous revision.",
        {
            "action": _str("Rollout action.", ["status", "restart", "undo"]),
            "deployment": _str("Deployment name."),
            "namespace": NAMESPACE,
        },
        ["action", "deployment"],
    ),
    _spec(
        "delete",
        "Delete a pod (its controller recreates it) or a NetworkPolicy.",
        {
            "kind": _str("Resource kind.", DELETE_KINDS),
            "name": _str("Resource name."),
            "namespace": NAMESPACE,
        },
        ["kind", "name"],
    ),
    ToolSpec(
        FINAL_TOOL,
        "Call once the incident is resolved and you've confirmed the app is healthy. "
        "Ends the incident.",
        {
            "type": "object",
            "properties": {
                "root_cause": _str(
                    "What caused the incident, specifically (which object, what was wrong)."
                ),
                "fix_summary": _str("What you changed to fix it."),
            },
            "required": ["root_cause", "fix_summary"],
            "additionalProperties": False,
        },
    ),
]


@dataclass
class Mutation:
    tool: str
    kind: str
    name: str
    namespace: str
    diff: str


class AgentTools:
    """Executes tool calls against the cluster as the agent."""

    def __init__(self, k: Kube):
        self.k = k
        self.networking = client.NetworkingV1Api(k.api_client)
        self.mutations: list[Mutation] = []
        self._handlers: dict[str, Callable[[dict], str]] = {
            "get_resources": self.get_resources,
            "describe": self.describe,
            "get_logs": self.get_logs,
            "get_events": self.get_events,
            "query_metrics": self.query_metrics,
            "apply_patch": self.apply_patch,
            "rollout": self.rollout,
            "delete": self.delete,
        }

    @property
    def specs(self) -> list[ToolSpec]:
        return SPECS

    def run(self, name: str, args: dict) -> str:
        handler = self._handlers.get(name)
        if not handler:
            return f"error: unknown tool {name!r}"
        args = {k: v for k, v in args.items() if k != "purpose"}
        try:
            return truncate(handler(args))
        except ToolError as exc:
            return f"error: {exc}"
        except ApiException as exc:
            return api_error(exc)
        except Exception as exc:
            return f"error: {type(exc).__name__}: {exc}"

    # --- argument helpers ---

    @staticmethod
    def _ns(args: dict) -> str:
        ns = args.get("namespace") or "shop"
        if ns not in NAMESPACES:
            raise ToolError(f"namespace {ns!r} is not allowed; use one of {NAMESPACES}")
        return ns

    @staticmethod
    def _kind(args: dict, allowed: list[str]) -> str:
        kind = str(args.get("kind", "")).lower()
        if kind not in allowed:
            raise ToolError(f"kind {kind!r} is not allowed here; use one of {allowed}")
        return kind

    @staticmethod
    def _name(args: dict, key: str = "name") -> str:
        name = args.get(key)
        if not name or not isinstance(name, str):
            raise ToolError(f"missing {key!r}")
        return name

    # --- reads ---

    def _read(self, kind: str, name: str, ns: str):
        k = self.k
        return {
            "pod": lambda: k.core.read_namespaced_pod(name, ns),
            "deployment": lambda: k.apps.read_namespaced_deployment(name, ns),
            "replicaset": lambda: k.apps.read_namespaced_replica_set(name, ns),
            "service": lambda: k.core.read_namespaced_service(name, ns),
            "configmap": lambda: k.core.read_namespaced_config_map(name, ns),
            "secret": lambda: k.core.read_namespaced_secret(name, ns),
            "networkpolicy": lambda: self.networking.read_namespaced_network_policy(name, ns),
        }[kind]()

    def get_resources(self, args: dict) -> str:
        kind, ns = self._kind(args, LIST_KINDS), self._ns(args)
        k = self.k
        if kind == "pods":
            rows = []
            for p in k.core.list_namespaced_pod(ns).items:
                cs = p.status.container_statuses or []
                ready = sum(1 for c in cs if c.ready)
                status = p.status.phase
                for c in cs:
                    if c.state and c.state.waiting and c.state.waiting.reason:
                        status = c.state.waiting.reason
                if p.metadata.deletion_timestamp:
                    status = "Terminating"
                restarts = sum(c.restart_count for c in cs)
                rows.append(
                    [
                        p.metadata.name,
                        f"{ready}/{len(p.spec.containers)}",
                        status,
                        restarts,
                        age(p.metadata.creation_timestamp),
                        p.spec.node_name or "-",
                    ]
                )
            return table(["NAME", "READY", "STATUS", "RESTARTS", "AGE", "NODE"], rows)
        if kind == "deployments":
            rows = []
            for d in k.apps.list_namespaced_deployment(ns).items:
                st = d.status
                images = ",".join(c.image for c in d.spec.template.spec.containers)
                rows.append(
                    [
                        d.metadata.name,
                        f"{st.ready_replicas or 0}/{d.spec.replicas}",
                        st.updated_replicas or 0,
                        st.available_replicas or 0,
                        age(d.metadata.creation_timestamp),
                        images,
                    ]
                )
            return table(["NAME", "READY", "UP-TO-DATE", "AVAILABLE", "AGE", "IMAGES"], rows)
        if kind == "replicasets":
            rows = []
            for r in k.apps.list_namespaced_replica_set(ns).items:
                rev = (r.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "?")
                rows.append(
                    [
                        r.metadata.name,
                        r.spec.replicas,
                        r.status.replicas or 0,
                        r.status.ready_replicas or 0,
                        rev,
                        age(r.metadata.creation_timestamp),
                    ]
                )
            return table(["NAME", "DESIRED", "CURRENT", "READY", "REVISION", "AGE"], rows)
        if kind == "services":
            rows = []
            for s in k.core.list_namespaced_service(ns).items:
                ports = ",".join(f"{p.port}->{p.target_port}" for p in s.spec.ports or [])
                selector = ",".join(f"{a}={b}" for a, b in (s.spec.selector or {}).items())
                rows.append(
                    [s.metadata.name, s.spec.type, s.spec.cluster_ip, ports, selector or "-"]
                )
            return table(["NAME", "TYPE", "CLUSTER-IP", "PORTS", "SELECTOR"], rows)
        if kind == "endpointslices":
            rows = []
            for s in k.discovery.list_namespaced_endpoint_slice(ns).items:
                eps = s.endpoints or []
                ready = [e for e in eps if e.conditions and e.conditions.ready]
                svc = (s.metadata.labels or {}).get("kubernetes.io/service-name", "-")
                addrs = ",".join(a for e in ready for a in e.addresses) or "-"
                rows.append([s.metadata.name, svc, f"{len(ready)}/{len(eps)}", addrs])
            return table(["NAME", "SERVICE", "READY", "READY-ADDRESSES"], rows)
        if kind == "configmaps":
            rows = [
                [c.metadata.name, ",".join(sorted((c.data or {}).keys())) or "-"]
                for c in k.core.list_namespaced_config_map(ns).items
            ]
            return table(["NAME", "KEYS"], rows)
        if kind == "secrets":
            rows = [
                [s.metadata.name, s.type, ",".join(sorted((s.data or {}).keys())) or "-"]
                for s in k.core.list_namespaced_secret(ns).items
            ]
            return table(["NAME", "TYPE", "KEYS (values hidden)"], rows)
        # networkpolicies
        rows = []
        for n in self.networking.list_namespaced_network_policy(ns).items:
            sel = n.spec.pod_selector.match_labels if n.spec.pod_selector else None
            rows.append(
                [n.metadata.name, json.dumps(sel or {}), ",".join(n.spec.policy_types or [])]
            )
        return table(["NAME", "POD-SELECTOR", "POLICY-TYPES"], rows)

    def describe(self, args: dict) -> str:
        kind, ns, name = self._kind(args, DESCRIBE_KINDS), self._ns(args), self._name(args)
        obj = clean(to_dict(self._read(kind, name, ns)))
        if kind == "secret":
            obj = redact_secret(obj)
        out = yaml.safe_dump(obj, sort_keys=False, width=120)
        if kind == "pod":
            out += "\nEvents:\n" + self._events(ns, field_selector=f"involvedObject.name={name}")
        return out

    def get_logs(self, args: dict) -> str:
        ns, pod = self._ns(args), self._name(args, "pod")
        lines = max(1, min(int(args.get("tail_lines") or 100), MAX_LOG_LINES))
        logs = self.k.core.read_namespaced_pod_log(
            pod, ns, tail_lines=lines, previous=bool(args.get("previous", False))
        )
        return logs or "(no log output)"

    def _events(self, ns: str, field_selector: str | None = None, limit: int = 40) -> str:
        kwargs = {"field_selector": field_selector} if field_selector else {}
        events = self.k.core.list_namespaced_event(ns, **kwargs).items

        def when(e):
            return e.last_timestamp or e.event_time or e.metadata.creation_timestamp

        events = sorted(events, key=lambda e: when(e) or datetime.min.replace(tzinfo=UTC))[-limit:]
        rows = [
            [
                age(when(e)),
                e.type,
                e.reason,
                f"{e.involved_object.kind.lower()}/{e.involved_object.name}",
                f"(x{e.count}) " * bool(e.count and e.count > 1) + (e.message or "").strip(),
            ]
            for e in events
        ]
        return table(["AGE", "TYPE", "REASON", "OBJECT", "MESSAGE"], rows)

    def get_events(self, args: dict) -> str:
        return self._events(self._ns(args))

    def query_metrics(self, args: dict) -> str:
        promql = args.get("promql")
        if not promql:
            raise ToolError("missing 'promql'")
        api = self.k.api_client
        request = api.param_serialize(
            "GET",
            "/api/v1/namespaces/monitoring/services/prometheus:9090/proxy/api/v1/query",
            query_params=[("query", promql)],
            auth_settings=["BearerToken"],
        )
        resp = api.call_api(*request, _request_timeout=15)
        resp.read()
        body = json.loads(resp.data)
        if body.get("status") != "success":
            return f"error: {body.get('errorType')}: {body.get('error')}"
        data = body["data"]
        results = data["result"]
        if data["resultType"] in ("scalar", "string"):
            return str(results[1])
        if not results:
            return "(no data)"
        lines = []
        for r in results[:50]:
            labels = ",".join(f'{k}="{v}"' for k, v in sorted(r["metric"].items()))
            value = r["value"][1] if "value" in r else f"{len(r.get('values', []))} samples"
            lines.append(f"{{{labels}}} {value}")
        if len(results) > 50:
            lines.append(f"... {len(results) - 50} more series")
        return "\n".join(lines)

    # --- writes ---

    def _patch(self, kind: str, name: str, ns: str, body, dry_run: bool):
        k = self.k
        kwargs = {"dry_run": "All"} if dry_run else {}
        return {
            "deployment": lambda: k.apps.patch_namespaced_deployment(name, ns, body, **kwargs),
            "service": lambda: k.core.patch_namespaced_service(name, ns, body, **kwargs),
            "configmap": lambda: k.core.patch_namespaced_config_map(name, ns, body, **kwargs),
            "networkpolicy": lambda: self.networking.patch_namespaced_network_policy(
                name, ns, body, **kwargs
            ),
        }[kind]()

    def _apply(self, tool: str, kind: str, name: str, ns: str, body) -> str:
        """Dry-run, diff, then apply. Records the mutation."""
        before = to_dict(self._read(kind, name, ns))
        preview = to_dict(self._patch(kind, name, ns, body, dry_run=True))
        change = diff(kind, before, preview)
        if not change:
            return "no change: the patch leaves the object as it is"
        self._patch(kind, name, ns, body, dry_run=False)
        self.mutations.append(Mutation(tool, kind, name, ns, change))
        return f"applied {kind}/{name}:\n{change}"

    def apply_patch(self, args: dict) -> str:
        kind, ns, name = self._kind(args, PATCH_KINDS), self._ns(args), self._name(args)
        patch = args.get("patch")
        if isinstance(patch, str):
            try:
                patch = json.loads(patch)
            except json.JSONDecodeError as exc:
                raise ToolError(f"patch is not valid JSON: {exc}") from exc
        if not isinstance(patch, dict) or not patch:
            raise ToolError("patch must be a non-empty JSON object")
        return self._apply("apply_patch", kind, name, ns, patch)

    def rollout(self, args: dict) -> str:
        ns, name = self._ns(args), self._name(args, "deployment")
        action = args.get("action")
        if action == "status":
            dep = self.k.apps.read_namespaced_deployment(name, ns)
            st = dep.status
            conds = "\n".join(
                f"  {c.type}={c.status} ({c.reason}): {c.message}" for c in st.conditions or []
            )
            return (
                f"deployment/{name}: replicas={dep.spec.replicas} "
                f"updated={st.updated_replicas or 0} ready={st.ready_replicas or 0} "
                f"available={st.available_replicas or 0} "
                f"unavailable={st.unavailable_replicas or 0}\nconditions:\n{conds}"
            )
        if action == "restart":
            stamp = datetime.now(UTC).isoformat()
            body = {
                "spec": {
                    "template": {
                        "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}
                    }
                }
            }
            self._patch("deployment", name, ns, body, dry_run=False)
            self.mutations.append(Mutation("rollout", "deployment", name, ns, "restart"))
            return f"deployment/{name} restarted"
        if action == "undo":
            return self._undo(name, ns)
        raise ToolError("action must be one of: status, restart, undo")

    def _undo(self, name: str, ns: str) -> str:
        """Same as `kubectl rollout undo`: put back the previous revision's pod template."""
        dep = self.k.apps.read_namespaced_deployment(name, ns)
        current = int((dep.metadata.annotations or {}).get("deployment.kubernetes.io/revision", 0))
        selector = ",".join(f"{a}={b}" for a, b in dep.spec.selector.match_labels.items())
        owned = [
            rs
            for rs in self.k.apps.list_namespaced_replica_set(ns, label_selector=selector).items
            if any(o.uid == dep.metadata.uid for o in rs.metadata.owner_references or [])
        ]
        older = [
            (int((rs.metadata.annotations or {}).get("deployment.kubernetes.io/revision", 0)), rs)
            for rs in owned
        ]
        older = sorted((r for r in older if r[0] < current), key=lambda r: r[0])
        if not older:
            raise ToolError(f"deployment/{name} has no previous revision to roll back to")
        revision, rs = older[-1]
        template = to_dict(rs.spec.template)
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        body = [{"op": "replace", "path": "/spec/template", "value": template}]
        result = self._apply("rollout", "deployment", name, ns, body)
        return f"rolled back to revision {revision}. {result}"

    def delete(self, args: dict) -> str:
        kind, ns, name = self._kind(args, DELETE_KINDS), self._ns(args), self._name(args)
        if kind == "pod":
            self.k.core.delete_namespaced_pod(name, ns)
        else:
            self.networking.delete_namespaced_network_policy(name, ns)
        self.mutations.append(Mutation("delete", kind, name, ns, "deleted"))
        return f"deleted {kind}/{name}"
