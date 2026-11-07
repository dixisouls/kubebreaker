"""The fault catalog: easy and medium tiers.

Each fault is self-contained: how to break the app, how to tell the break is visible,
the alert the agent gets, the ground truth, and a reference fix for selftest.
"""

from kubernetes import client

from kubebreaker.chaos.base import Fault, Tier, register
from kubebreaker.chaos.checks import frontend_problems, has_unready_running_pod, pod_waiting_reasons
from kubebreaker.kube import Kube

GOOD_IMAGE = "kubebreaker/shop:0.1.0"


def container_patch(name: str, **fields) -> dict:
    """Strategic-merge patch for one container in a Deployment's pod template."""
    return {"spec": {"template": {"spec": {"containers": [{"name": name, **fields}]}}}}


def frontend_failing(k: Kube) -> bool:
    return bool(frontend_problems(k))


# --- Easy -------------------------------------------------------------------------------

register(
    Fault(
        id="bad-command",
        tier=Tier.EASY,
        title="api container command points at a module that doesn't exist",
        alert="KubePodCrashLooping: pods of deployment shop/api are restarting repeatedly.",
        ground_truth=(
            "The api Deployment's container command was changed to load 'shop.gateway:app', "
            "a module that doesn't exist, so new api pods crash on startup."
        ),
        expected_fix="Restore the api command to 'uvicorn shop.api:app ...' or roll back.",
        inject=lambda k: k.patch_deployment(
            "api",
            container_patch(
                "api",
                command=["uvicorn", "shop.gateway:app", "--host", "0.0.0.0", "--port", "8080"],
            ),
        ),
        symptom=lambda k: "CrashLoopBackOff" in pod_waiting_reasons(k, "api"),
        reference_fix=lambda k: k.patch_deployment(
            "api",
            container_patch(
                "api", command=["uvicorn", "shop.api:app", "--host", "0.0.0.0", "--port", "8080"]
            ),
        ),
    )
)

register(
    Fault(
        id="bad-image-tag",
        tier=Tier.EASY,
        title="frontend image tag doesn't exist",
        alert="KubeDeploymentRolloutStuck: deployment shop/frontend has not finished rolling out.",
        ground_truth=(
            "The frontend Deployment's image was changed to kubebreaker/shop:0.2.0, a tag "
            "that doesn't exist, so new frontend pods can't pull their image."
        ),
        expected_fix="Set the frontend image back to kubebreaker/shop:0.1.0 or roll back.",
        inject=lambda k: k.patch_deployment(
            "frontend", container_patch("frontend", image="kubebreaker/shop:0.2.0")
        ),
        symptom=lambda k: bool(
            {"ErrImagePull", "ImagePullBackOff"} & pod_waiting_reasons(k, "frontend")
        ),
        reference_fix=lambda k: k.patch_deployment(
            "frontend", container_patch("frontend", image=GOOD_IMAGE)
        ),
    )
)

register(
    Fault(
        id="scaled-to-zero",
        tier=Tier.EASY,
        title="api scaled to zero replicas",
        alert="HighErrorRate: the shop frontend is returning 5xx errors for most requests.",
        ground_truth=(
            "The api Deployment was scaled to 0 replicas, so the api Service has no endpoints."
        ),
        expected_fix="Scale the api Deployment back to 2 replicas.",
        inject=lambda k: k.apps.patch_namespaced_deployment_scale(
            "api", k.namespace, {"spec": {"replicas": 0}}
        ),
        symptom=frontend_failing,
        reference_fix=lambda k: k.apps.patch_namespaced_deployment_scale(
            "api", k.namespace, {"spec": {"replicas": 2}}
        ),
    )
)

# --- Medium -----------------------------------------------------------------------------


def _set_db_host(k: Kube, host: str) -> None:
    k.core.patch_namespaced_config_map("orders-config", k.namespace, {"data": {"DB_HOST": host}})
    # Env vars from a ConfigMap are only read at container start
    k.restart_deployment("orders")


register(
    Fault(
        id="wrong-db-host",
        tier=Tier.MEDIUM,
        title="orders ConfigMap points at a database host that doesn't exist",
        alert="HighErrorRate: the shop frontend is returning 5xx errors for most requests.",
        ground_truth=(
            "DB_HOST in the orders-config ConfigMap was changed to 'postgres-primary', which "
            "doesn't resolve, so orders can't reach Postgres. The api returns 502 and the "
            "frontend fails."
        ),
        expected_fix="Set DB_HOST back to 'postgres' in orders-config and restart orders.",
        inject=lambda k: _set_db_host(k, "postgres-primary"),
        symptom=frontend_failing,
        reference_fix=lambda k: _set_db_host(k, "postgres"),
        extra_checks=lambda k: (
            []
            if k.core.read_namespaced_config_map("orders-config", k.namespace).data["DB_HOST"]
            == "postgres"
            else ["orders-config DB_HOST is not 'postgres'"]
        ),
    )
)

register(
    Fault(
        id="oom-killed",
        tier=Tier.MEDIUM,
        title="orders memory limit far too low",
        alert="KubePodCrashLooping: pods of deployment shop/orders are restarting repeatedly.",
        ground_truth=(
            "The orders container's memory limit was lowered to 24Mi (request 16Mi), below what "
            "the process needs to start, so new orders pods are OOMKilled."
        ),
        expected_fix="Raise the orders memory limit back to 256Mi (or another sane value).",
        inject=lambda k: k.patch_deployment(
            "orders",
            container_patch(
                "orders",
                resources={"requests": {"memory": "16Mi"}, "limits": {"memory": "24Mi"}},
            ),
        ),
        symptom=lambda k: "OOMKilled" in pod_waiting_reasons(k, "orders"),
        reference_fix=lambda k: k.patch_deployment(
            "orders",
            container_patch(
                "orders",
                resources={"requests": {"memory": "64Mi"}, "limits": {"memory": "256Mi"}},
            ),
        ),
    )
)

register(
    Fault(
        id="broken-selector",
        tier=Tier.MEDIUM,
        title="orders Service selector matches no pods",
        alert="HighErrorRate: the shop frontend is returning 5xx errors for most requests.",
        ground_truth=(
            "The orders Service selector was changed to app=order, which matches no pods, so "
            "the Service has no endpoints even though the orders pods are healthy."
        ),
        expected_fix="Set the orders Service selector back to app=orders.",
        inject=lambda k: k.core.patch_namespaced_service(
            "orders", k.namespace, {"spec": {"selector": {"app": "order"}}}
        ),
        symptom=frontend_failing,
        reference_fix=lambda k: k.core.patch_namespaced_service(
            "orders", k.namespace, {"spec": {"selector": {"app": "orders"}}}
        ),
    )
)

register(
    Fault(
        id="failing-readiness",
        tier=Tier.MEDIUM,
        title="frontend readiness probe checks a path that doesn't exist",
        alert="KubeDeploymentRolloutStuck: deployment shop/frontend has not finished rolling out.",
        ground_truth=(
            "The frontend readiness probe path was changed to /ready, which returns 404, so new "
            "frontend pods run but never become Ready."
        ),
        expected_fix="Set the frontend readiness probe path back to /readyz.",
        inject=lambda k: k.patch_deployment(
            "frontend",
            container_patch(
                "frontend", readinessProbe={"httpGet": {"path": "/ready", "port": 8080}}
            ),
        ),
        symptom=lambda k: has_unready_running_pod(k, "frontend"),
        reference_fix=lambda k: k.patch_deployment(
            "frontend",
            container_patch(
                "frontend", readinessProbe={"httpGet": {"path": "/readyz", "port": 8080}}
            ),
        ),
    )
)


def _set_orders_secret(k: Kube, secret_name: str) -> None:
    """Point both of orders' secret-backed env vars at `secret_name`."""
    dep = k.apps.read_namespaced_deployment("orders", k.namespace)
    container = next(c for c in dep.spec.template.spec.containers if c.name == "orders")
    env = []
    for var in container.env:
        if var.value_from and var.value_from.secret_key_ref:
            var.value_from.secret_key_ref.name = secret_name
        env.append(client.ApiClient().sanitize_for_serialization(var))
    k.patch_deployment("orders", container_patch("orders", env=env))


register(
    Fault(
        id="missing-secret-ref",
        tier=Tier.MEDIUM,
        title="orders references a Secret that doesn't exist",
        alert="KubeDeploymentRolloutStuck: deployment shop/orders has not finished rolling out.",
        ground_truth=(
            "The orders Deployment's DB_USER and DB_PASSWORD now reference a Secret named "
            "'orders-db-credentials', which doesn't exist (the real one is "
            "'postgres-credentials'), so new orders pods fail with CreateContainerConfigError."
        ),
        expected_fix="Point the orders env vars back at the postgres-credentials Secret.",
        inject=lambda k: _set_orders_secret(k, "orders-db-credentials"),
        symptom=lambda k: "CreateContainerConfigError" in pod_waiting_reasons(k, "orders"),
        reference_fix=lambda k: _set_orders_secret(k, "postgres-credentials"),
    )
)
