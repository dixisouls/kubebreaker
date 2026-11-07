"""End-to-end fault selftests. Needs the cluster: run with `uv run pytest -m cluster`."""

import pytest

from kubebreaker.chaos import CATALOG, runner
from kubebreaker.kube import Kube

pytestmark = pytest.mark.cluster


@pytest.fixture(scope="module")
def kube():
    try:
        k = Kube.from_env()
        k.core.list_namespace(_request_timeout=5)
    except Exception as exc:
        pytest.skip(f"cluster not available: {exc}")
    yield k
    runner.reset_app(k)


@pytest.mark.parametrize("fault_id", sorted(CATALOG))
def test_fault_injects_and_recovers(kube, fault_id):
    result = runner.selftest(kube, CATALOG[fault_id])
    assert result.ok, f"{result.error} {result.problems}"
