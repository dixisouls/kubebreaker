"""Hard-tier faults: the symptom shows up somewhere other than the cause, so the agent has to
trace the failure across services (or out of the app entirely) instead of pattern matching."""

import difflib

from kubernetes import client

from kubebreaker.chaos.base import Fault, Tier, register
from kubebreaker.chaos.checks import frontend_problems
from kubebreaker.chaos.faults import container_patch, frontend_failing
from kubebreaker.chaos.quality import cpu_limit_check, redis_memory_check
from kubebreaker.kube import Kube, wait_for

# --- netpol-block -----------------------------------------------------------------------

POLICY = "orders-allow-frontend"


def _create_policy(k: Kube) -> None:
    # Looks like a sensible hardening change, but the api (not the frontend) calls orders
    client.NetworkingV1Api(k.api_client).create_namespaced_network_policy(
        k.namespace,
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": POLICY},
            "spec": {
                "podSelector": {"matchLabels": {"app": "orders"}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [{"podSelector": {"matchLabels": {"app": "frontend"}}}],
                        "ports": [{"port": 8080}],
                    }
                ],
            },
        },
    )


register(
    Fault(
        id="netpol-block",
        tier=Tier.HARD,
        title="NetworkPolicy lets only the frontend reach orders, cutting off the api",
        alert="HighErrorRate: the shop frontend is returning 5xx errors for most requests.",
        ground_truth=(
            "A NetworkPolicy 'orders-allow-frontend' allows ingress to the orders pods only "
            "from app=frontend. The api is the service that actually calls orders, so its "
            "connections time out; all pods stay healthy and the frontend returns errors."
        ),
        expected_fix=(
            "Delete the orders-allow-frontend NetworkPolicy, or change it to allow ingress "
            "from app=api."
        ),
        inject=_create_policy,
        symptom=frontend_failing,
        reference_fix=lambda k: client.NetworkingV1Api(
            k.api_client
        ).delete_namespaced_network_policy(POLICY, k.namespace),
        fix_scope=(f"networkpolicy/{POLICY}",),
    )
)

# --- dns-namespace-filter ---------------------------------------------------------------

FILTER_LINE = "       namespaces kube-system\n"


def _corefile(k: Kube, namespace: str, name: str) -> str:
    return k.core.read_namespaced_config_map(name, namespace).data["Corefile"]


def _set_corefile(k: Kube, corefile: str) -> None:
    k.core.patch_namespaced_config_map("coredns", "kube-system", {"data": {"Corefile": corefile}})
    # Restart rather than wait up to a minute or two for CoreDNS's reload plugin
    _restart(k, "coredns", "kube-system")


def _restart(k: Kube, deployment: str, namespace: str) -> None:
    Kube(k.context, namespace, k.api_client).restart_deployment(deployment)
    wait_for(lambda: _rolled_out(k, deployment, namespace), timeout=120)


def _rolled_out(k: Kube, deployment: str, namespace: str) -> bool:
    d = k.apps.read_namespaced_deployment(deployment, namespace)
    st = d.status
    return (
        (st.observed_generation or 0) >= d.metadata.generation
        and (st.updated_replicas or 0) == d.spec.replicas
        and (st.available_replicas or 0) == d.spec.replicas
        and (st.replicas or 0) == d.spec.replicas
    )


def _baseline_corefile(k: Kube) -> str:
    return _corefile(k, "kubebreaker", "coredns-baseline")


def _break_dns(k: Kube) -> None:
    base = _baseline_corefile(k)
    if "       pods insecure\n" not in base:
        raise RuntimeError("unexpected Corefile layout; can't inject dns-namespace-filter")
    _set_corefile(k, base.replace("       pods insecure\n", "       pods insecure\n" + FILTER_LINE))
    # orders keeps working on its open DB connections; a restart forces a fresh lookup
    _restart(k, "orders", k.namespace)


def _filter_removed(k: Kube) -> list[str]:
    if FILTER_LINE.strip() in _corefile(k, "kube-system", "coredns"):
        return ["CoreDNS still only serves kube-system ('namespaces kube-system')"]
    return []


COREFILE = "kube-system/configmap/coredns"


def corefile_collateral(final: dict, baseline: dict) -> list[str]:
    """Edits to the Corefile beyond removing the injected line. Each changed line counts."""
    want = (baseline.get(COREFILE, {}).get("data.Corefile") or "").splitlines()
    have = (final.get(COREFILE, {}).get("data.Corefile") or "").splitlines()
    have = [line for line in have if line.strip() != FILTER_LINE.strip()]
    changed = [
        line
        for line in difflib.unified_diff(want, have, lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    if not changed:
        return []
    return [f"CoreDNS Corefile also changed: {' | '.join(c.strip() for c in changed[:6])}"]


register(
    Fault(
        id="dns-namespace-filter",
        tier=Tier.HARD,
        title="CoreDNS only serves records for kube-system",
        alert="HighErrorRate: the shop frontend is returning 5xx errors for most requests.",
        ground_truth=(
            "The CoreDNS Corefile's kubernetes plugin was given 'namespaces kube-system', so "
            "cluster DNS stops answering for services in the shop namespace. After orders "
            "restarted it can't resolve 'postgres', even though its DB_HOST setting is correct."
        ),
        expected_fix=(
            "Remove the 'namespaces kube-system' line from the CoreDNS ConfigMap in kube-system "
            "(and restart CoreDNS, or wait for it to reload)."
        ),
        inject=_break_dns,
        symptom=frontend_failing,
        reference_fix=lambda k: _set_corefile(k, _baseline_corefile(k)),
        extra_checks=_filter_removed,
        collateral_checks=corefile_collateral,
        fix_scope=("kube-system/configmap/coredns:data.Corefile",),
        allowed_restarts=("coredns", "orders"),
    )
)

# --- redis-oom --------------------------------------------------------------------------

REDIS_BASE_ARGS = ["--save", "", "--appendonly", "no"]
REDIS_GOOD = [*REDIS_BASE_ARGS, "--maxmemory", "64mb", "--maxmemory-policy", "allkeys-lru"]
REDIS_FULL = [*REDIS_BASE_ARGS, "--maxmemory", "1mb", "--maxmemory-policy", "noeviction"]

register(
    Fault(
        id="redis-oom",
        tier=Tier.HARD,
        title="Redis memory cap below its idle usage, with eviction off",
        alert="HighErrorRate: the shop frontend is returning 5xx errors for most requests.",
        ground_truth=(
            "The redis Deployment's args set --maxmemory 1mb with --maxmemory-policy "
            "noeviction. Redis already uses more than 1MB at idle, so every cache write fails "
            "with an OOM error, the api returns 503 and the frontend fails, while the Redis "
            "pod itself looks healthy."
        ),
        expected_fix="Restore the redis args to --maxmemory 64mb --maxmemory-policy allkeys-lru.",
        inject=lambda k: k.patch_deployment("redis", container_patch("redis", args=REDIS_FULL)),
        symptom=frontend_failing,
        reference_fix=lambda k: k.patch_deployment(
            "redis", container_patch("redis", args=REDIS_GOOD)
        ),
        fix_scope=("deployment/redis:spec.template.spec.containers[redis].args",),
        quality_checks=redis_memory_check,
    )
)

# --- cpu-starvation ---------------------------------------------------------------------

FRONTEND_P95 = (
    "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
    '{service="frontend"}[30s])))'
)


def sustained(fn: str) -> str:
    """p95 sampled every 5s over the last 30s, reduced with min or max, so one spike or one
    quiet moment can't decide the result."""
    return f"{fn}_over_time(({FRONTEND_P95})[30s:5s])"


# The healthy p95 is about 20ms; starved, it swings between ~140ms and ~340ms
SLOW_SECONDS = 0.12  # symptom threshold
OK_SECONDS = 0.06  # recovered threshold


def _latency_problems(k: Kube) -> list[str]:
    worst = k.prom_value(sustained("max"))
    if worst is None:
        return ["no frontend latency data"]
    if worst > OK_SECONDS:
        return [
            f"frontend p95 latency peaked at {worst * 1000:.0f}ms in the last 30s "
            f"(needs to stay under {OK_SECONDS * 1000:.0f}ms)"
        ]
    return []


def _latency_high(k: Kube) -> bool:
    best = k.prom_value(sustained("min"))
    return best is not None and best > SLOW_SECONDS and not frontend_problems(k)


register(
    Fault(
        id="cpu-starvation",
        tier=Tier.HARD,
        title="Redis CPU limit near zero",
        alert=(
            "HighLatency: shop frontend p95 latency is above 100ms (normally about 20ms). "
            "Error rate is normal."
        ),
        ground_truth=(
            "The redis container got a CPU limit of 1m (request 1m), so it is heavily "
            "CPU-throttled. Every api request waits on Redis, so frontend latency rises "
            "roughly 7-15x with no errors anywhere."
        ),
        expected_fix=(
            "Remove the redis CPU limit (the baseline has none) or raise it to a sane value, "
            "and restore the 20m CPU request."
        ),
        inject=lambda k: k.patch_deployment(
            "redis",
            container_patch(
                "redis", resources={"requests": {"cpu": "1m"}, "limits": {"cpu": "1m"}}
            ),
        ),
        symptom=_latency_high,
        reference_fix=lambda k: k.patch_deployment(
            "redis",
            # null removes the field in a strategic merge patch
            container_patch(
                "redis", resources={"requests": {"cpu": "20m"}, "limits": {"cpu": None}}
            ),
        ),
        extra_checks=_latency_problems,
        fix_scope=("deployment/redis:spec.template.spec.containers[redis].resources",),
        quality_checks=lambda snap: cpu_limit_check(snap, "redis"),
    )
)
