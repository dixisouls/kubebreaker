"""Kubernetes access pinned to the kubebreaker kind cluster."""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property

from kubernetes import client, config

DEFAULT_CONTEXT = "kind-kubebreaker"
APP_NAMESPACE = "shop"


class WrongClusterError(RuntimeError):
    pass


@dataclass
class Kube:
    """API clients bound to one kube context. Refuses anything that isn't a kind cluster."""

    context: str = DEFAULT_CONTEXT
    namespace: str = APP_NAMESPACE

    def __post_init__(self):
        if not self.context.startswith("kind-"):
            raise WrongClusterError(f"refusing to use non-kind context {self.context!r}")
        contexts, _ = config.list_kube_config_contexts()
        if self.context not in {c["name"] for c in contexts}:
            raise WrongClusterError(f"kube context {self.context!r} not found; run make cluster-up")
        self._api_client = config.new_client_from_config(context=self.context)

    @classmethod
    def from_env(cls) -> "Kube":
        return cls(context=os.environ.get("KUBE_CONTEXT", DEFAULT_CONTEXT))

    @cached_property
    def core(self) -> client.CoreV1Api:
        return client.CoreV1Api(self._api_client)

    @cached_property
    def apps(self) -> client.AppsV1Api:
        return client.AppsV1Api(self._api_client)

    @cached_property
    def discovery(self) -> client.DiscoveryV1Api:
        return client.DiscoveryV1Api(self._api_client)

    # --- helpers shared by faults and checks ---

    def patch_deployment(self, name: str, body: dict) -> None:
        self.apps.patch_namespaced_deployment(name, self.namespace, body)

    def restart_deployment(self, name: str) -> None:
        """Same as `kubectl rollout restart`."""
        stamp = datetime.now(UTC).isoformat()
        self.patch_deployment(
            name,
            {
                "spec": {
                    "template": {
                        "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}
                    }
                }
            },
        )

    def deployment_pods(self, name: str) -> list[client.V1Pod]:
        dep = self.apps.read_namespaced_deployment(name, self.namespace)
        selector = ",".join(f"{k}={v}" for k, v in dep.spec.selector.match_labels.items())
        return self.core.list_namespaced_pod(self.namespace, label_selector=selector).items

    def frontend_page(self) -> str | None:
        """GET the frontend through the API server proxy. Returns the body, or None on error."""
        try:
            return self.core.connect_get_namespaced_service_proxy_with_path(
                "frontend:80", self.namespace, path="", _request_timeout=10
            )
        except Exception:
            return None


def wait_for(check: Callable[[], bool], timeout: float, interval: float = 2.0) -> bool:
    """Poll `check` until it returns True or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if check():
                return True
        except Exception:
            pass  # transient API errors count as "not yet"
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
