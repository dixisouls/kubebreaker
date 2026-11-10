"""Alertmanager paging, with a fake Alertmanager. No cluster."""

from datetime import UTC, datetime, timedelta

import pytest
import yaml

from kubebreaker.chaos import CATALOG, alerting
from kubebreaker.chaos.runner import REPO_ROOT

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def am_alert(name, started=T0, description="something broke", **labels):
    return {
        "labels": {"alertname": name, "namespace": "shop", **labels},
        "annotations": {"description": description},
        "startsAt": started.isoformat().replace("+00:00", "Z"),
        "status": {"state": "active"},
    }


class FakeKube:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def service_get(self, service, path, params):
        self.calls.append((service, path, params))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def test_alert_names_come_from_each_line_prefix():
    text = "HighErrorRate: the frontend 5xx.\nKubePodCrashLooping: pods restarting."
    assert alerting.alert_names(text) == ["HighErrorRate", "KubePodCrashLooping"]
    assert alerting.alert_names("HighErrorRate: a\nHighErrorRate: b") == ["HighErrorRate"]


def test_every_catalog_alert_has_a_rule():
    rules = yaml.safe_load_all((REPO_ROOT / "cluster/monitoring/alerting.yaml").read_text())
    cm = next(d for d in rules if d["metadata"]["name"] == "prometheus-rules")
    groups = yaml.safe_load(cm["data"]["shop.yml"])["groups"]
    defined = {r["alert"] for g in groups for r in g["rules"]}
    for fault in CATALOG.values():
        missing = set(alerting.alert_names(fault.alert)) - defined
        assert not missing, f"{fault.id}: no rule for {missing}"


def test_firing_needs_every_name_and_a_new_start():
    alerts = [am_alert("HighErrorRate"), am_alert("HighLatency", T0 - timedelta(minutes=5))]
    assert alerting.firing(alerts, ["HighErrorRate"], since=T0 - timedelta(seconds=1))
    # HighLatency started before `since`: an old alert, not this fault's
    assert alerting.firing(alerts, ["HighErrorRate", "HighLatency"], since=T0) == []
    both = alerting.firing(alerts, ["HighLatency", "HighErrorRate"])
    assert [a["labels"]["alertname"] for a in both] == ["HighLatency", "HighErrorRate"]


def test_other_namespaces_and_suppressed_alerts_are_ignored():
    suppressed = am_alert("HighErrorRate")
    suppressed["status"]["state"] = "suppressed"
    k = FakeKube([am_alert("HighErrorRate", namespace="monitoring"), suppressed])
    assert alerting.active_alerts(k) == []
    assert k.calls[0][:2] == ("alertmanager:9093", "/api/v2/alerts")


def test_alert_text_matches_the_catalog_shape():
    a = am_alert(
        "KubePodCrashLooping", description="pods of deployment shop/api\n  are restarting."
    )
    assert (
        alerting.alert_text([a])
        == "KubePodCrashLooping: pods of deployment shop/api are restarting."
    )


def test_wait_firing_polls_until_the_alert_appears(monkeypatch):
    monkeypatch.setattr(alerting.time, "sleep", lambda s: None)
    k = FakeKube([], [], [am_alert("HighErrorRate")])
    found = alerting.wait_firing(k, ["HighErrorRate"], since=T0 - timedelta(seconds=30))
    assert len(found) == 1 and len(k.calls) == 3


def test_wait_clear_times_out_on_a_stuck_alert(monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(alerting.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(alerting.time, "sleep", lambda s: None)
    with pytest.raises(alerting.AlertTimeout, match="still firing"):
        alerting.wait_clear(FakeKube([am_alert("HighErrorRate")]), ["HighErrorRate"], timeout=60)


def test_alert_source_is_validated(monkeypatch):
    monkeypatch.delenv("ALERT_SOURCE", raising=False)
    assert alerting.alert_source() == "hold"
    monkeypatch.setenv("ALERT_SOURCE", "alertmanager")
    assert alerting.alert_source() == "alertmanager"
    assert alerting.alert_source("hold") == "hold"
    with pytest.raises(ValueError, match="alert source"):
        alerting.alert_source("pagerduty")
