"""Scoring against the real cluster. Run with `uv run pytest -m cluster`."""

import time

import pytest

from kubebreaker.chaos import CATALOG, runner
from kubebreaker.kube import Kube
from kubebreaker.scoring.diff import diff
from kubebreaker.scoring.score import score_fix
from kubebreaker.scoring.snapshot import take_snapshot

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


def test_snapshot_of_untouched_cluster_is_stable(kube):
    runner.reset_app(kube)
    first = take_snapshot(kube)
    time.sleep(10)
    assert diff(first, take_snapshot(kube)) == []


@pytest.mark.parametrize("fault_id", sorted(CATALOG))
def test_reference_fix_scores_clean(kube, fault_id):
    """The known-good fix must score 1.0 with no collateral, which proves fix_scope is right."""
    fault = CATALOG[fault_id]
    runner.reset_app(kube)
    baseline = take_snapshot(kube)
    runner.inject(kube, fault)
    injected = take_snapshot(kube)
    fault.reference_fix(kube)
    assert runner.verify(kube, fault) == []

    # The injection itself must land inside the fix scope, or the scope is wrong
    outside = score_fix(fault, True, baseline, injected, []).collateral
    assert outside == [], f"injection changed things outside fix_scope: {outside}"

    restarts = [
        {"tool": "rollout", "kind": "deployment", "name": n, "namespace": "shop", "diff": "restart"}
        for n in fault.allowed_restarts
    ]
    s = score_fix(fault, True, baseline, take_snapshot(kube), restarts)
    assert (s.quality, s.collateral, s.band_aids) == (1.0, [], []), s
