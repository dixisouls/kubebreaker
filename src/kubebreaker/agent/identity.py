"""Build a Kubernetes client that acts as the sre-agent ServiceAccount, not as the admin."""

import copy

from kubernetes import client

from kubebreaker.kube import Kube

SA_NAMESPACE = "kubebreaker"
SA_NAME = "sre-agent"
TOKEN_SECONDS = 3600


def agent_kube(admin: Kube) -> Kube:
    """A Kube whose every call is authorized as the sre-agent ServiceAccount (see
    cluster/agent/rbac.yaml). Uses a short-lived token from the TokenRequest API."""
    token = admin.core.create_namespaced_service_account_token(
        SA_NAME,
        SA_NAMESPACE,
        client.AuthenticationV1TokenRequest(
            spec=client.V1TokenRequestSpec(audiences=[], expiration_seconds=TOKEN_SECONDS)
        ),
    ).status.token

    cfg = copy.copy(admin.api_client.configuration)
    # Drop the admin's client certificate so only the token authenticates us
    cfg.cert_file = None
    cfg.key_file = None
    cfg.api_key = {"authorization": token}
    cfg.api_key_prefix = {"authorization": "Bearer"}
    return Kube(context=admin.context, namespace=admin.namespace, api_client=client.ApiClient(cfg))
