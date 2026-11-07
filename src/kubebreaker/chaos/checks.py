"""Health checks for the shop app. An app is healthy when it matches the baseline."""

from datetime import UTC, datetime

from kubebreaker.kube import Kube

BASELINE_DEPLOYMENTS = ["postgres", "redis", "orders", "api", "frontend", "loadgen"]
BASELINE_SERVICES = ["postgres", "redis", "orders", "api", "frontend"]
FRONTEND_MARKER = "Recent orders"


def deployment_problems(k: Kube, name: str) -> list[str]:
    """A Deployment is healthy when it has replicas and its latest rollout fully finished."""
    try:
        dep = k.apps.read_namespaced_deployment(name, k.namespace)
    except Exception:
        return [f"deployment/{name} missing"]
    want = dep.spec.replicas or 0
    st = dep.status
    if want == 0:
        return [f"deployment/{name} scaled to 0"]
    problems = []
    if (st.observed_generation or 0) < dep.metadata.generation:
        problems.append(f"deployment/{name} change not yet observed")
    if (st.updated_replicas or 0) != want:
        problems.append(f"deployment/{name} rollout incomplete ({st.updated_replicas or 0}/{want})")
    if (st.available_replicas or 0) != want:
        problems.append(f"deployment/{name} {st.available_replicas or 0}/{want} available")
    if (st.replicas or 0) != want:
        problems.append(f"deployment/{name} has {st.replicas} pods, wants {want}")
    return problems


def service_problems(k: Kube, name: str) -> list[str]:
    slices = k.discovery.list_namespaced_endpoint_slice(
        k.namespace, label_selector=f"kubernetes.io/service-name={name}"
    ).items
    ready = [
        ep for s in slices for ep in (s.endpoints or []) if ep.conditions and ep.conditions.ready
    ]
    return [] if ready else [f"service/{name} has no ready endpoints"]


def frontend_problems(k: Kube) -> list[str]:
    page = k.frontend_page()
    return [] if page and FRONTEND_MARKER in page else ["frontend does not render the orders page"]


def app_problems(k: Kube) -> list[str]:
    """Everything that differs from a healthy baseline. Empty list means healthy."""
    problems: list[str] = []
    for d in BASELINE_DEPLOYMENTS:
        problems += deployment_problems(k, d)
    for s in BASELINE_SERVICES:
        problems += service_problems(k, s)
    problems += frontend_problems(k)
    return problems


def pod_waiting_reasons(k: Kube, deployment: str) -> set[str]:
    """Waiting reasons (CrashLoopBackOff, ImagePullBackOff, ...) and last termination
    reasons (OOMKilled, Error) across the Deployment's pods."""
    reasons = set()
    for pod in k.deployment_pods(deployment):
        for cs in pod.status.container_statuses or []:
            if cs.state and cs.state.waiting and cs.state.waiting.reason:
                reasons.add(cs.state.waiting.reason)
            if cs.last_state and cs.last_state.terminated and cs.last_state.terminated.reason:
                reasons.add(cs.last_state.terminated.reason)
    return reasons


def has_unready_running_pod(k: Kube, deployment: str, min_age_seconds: float = 15) -> bool:
    """True if a pod has been running for a while without ever becoming Ready.
    New pods are briefly unready while their first probes run, so ignore young ones."""
    now = datetime.now(UTC)
    for pod in k.deployment_pods(deployment):
        for cs in pod.status.container_statuses or []:
            running = cs.state and cs.state.running
            age = (now - running.started_at).total_seconds() if running else 0
            if running and not cs.ready and age >= min_age_seconds:
                return True
    return False
