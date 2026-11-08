"""Catalog checks that don't need a cluster."""

import pytest

from kubebreaker.chaos import CATALOG, Tier

# The injected value for each fault. The alert must never contain it,
# otherwise the agent gets the root cause for free.
GIVEAWAYS = {
    "bad-command": ["gateway", "command", "module"],
    "bad-image-tag": ["0.2.0", "image", "tag"],
    "scaled-to-zero": ["replica", "scale", "api"],
    "wrong-db-host": ["postgres", "DB_HOST", "configmap"],
    "oom-killed": ["24Mi", "memory", "OOM"],
    "broken-selector": ["selector", "app=order", "endpoint"],
    "failing-readiness": ["/ready", "probe", "readiness"],
    "missing-secret-ref": ["secret", "orders-db-credentials", "CreateContainerConfigError"],
    "netpol-block": ["network", "policy", "orders", "timeout"],
    "dns-namespace-filter": ["dns", "coredns", "resolve", "postgres", "namespace"],
    "redis-oom": ["redis", "memory", "oom", "cache"],
    "cpu-starvation": ["cpu", "redis", "throttl", "limit"],
}


def test_every_fault_has_a_giveaway_list():
    assert set(GIVEAWAYS) == set(CATALOG)


@pytest.mark.parametrize("fault_id", sorted(CATALOG))
def test_alert_does_not_leak_the_cause(fault_id):
    alert = CATALOG[fault_id].alert.lower()
    for word in GIVEAWAYS[fault_id]:
        assert word.lower() not in alert, f"alert for {fault_id} mentions {word!r}"


@pytest.mark.parametrize("fault_id", sorted(CATALOG))
def test_fault_is_complete(fault_id):
    f = CATALOG[fault_id]
    assert f.id == fault_id
    assert isinstance(f.tier, Tier)
    for text in (f.title, f.alert, f.ground_truth, f.expected_fix):
        assert text.strip()
    for fn in (f.inject, f.symptom, f.reference_fix, f.extra_checks):
        assert callable(fn)


def test_catalog_covers_all_tiers():
    tiers = {f.tier for f in CATALOG.values()}
    assert tiers == {Tier.EASY, Tier.MEDIUM, Tier.HARD}
