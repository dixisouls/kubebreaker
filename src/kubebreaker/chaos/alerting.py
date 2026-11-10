"""Page the agent from Alertmanager instead of from the harness's own symptom poll.

Two alert sources:
  hold          (default) poll the fault's symptom until it has held for ALERT_HOLD_SECONDS,
                then send the fault's written alert text
  alertmanager  wait for Prometheus rules (cluster/monitoring/alerting.yaml) to fire and
                Alertmanager to report them, then send the text Alertmanager has
"""

import os
import re
import time
from datetime import UTC, datetime

from kubebreaker.kube import Kube

SOURCES = ("hold", "alertmanager")
ALERTS_PATH = "/api/v2/alerts"
SERVICE = "alertmanager:9093"
# Rules use `for: 45s` over a 1m rate window, and Prometheus evaluates every 15s
FIRE_TIMEOUT = 240
# An alert from the previous episode can take a rate window plus a few evaluations to resolve
CLEAR_TIMEOUT = 180


class AlertTimeout(RuntimeError):
    pass


def alert_source(value: str | None = None) -> str:
    source = value or os.environ.get("ALERT_SOURCE") or "hold"
    if source not in SOURCES:
        raise ValueError(f"alert source must be one of {', '.join(SOURCES)}, got {source!r}")
    return source


def alert_names(alert_text: str) -> list[str]:
    """Alert names a fault should raise: the `Name:` prefix of each line of its alert text."""
    names = re.findall(r"^\s*([A-Za-z]\w*):", alert_text, re.MULTILINE)
    return list(dict.fromkeys(names))


def _starts_at(alert: dict) -> datetime | None:
    raw = alert.get("startsAt")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def active_alerts(k: Kube) -> list[dict]:
    """Alerts Alertmanager currently holds as firing (not resolved, silenced or inhibited)."""
    params = [("active", "true"), ("silenced", "false"), ("inhibited", "false")]
    body = k.service_get(SERVICE, ALERTS_PATH, params)
    return [
        a
        for a in body or []
        if a.get("status", {}).get("state", "active") == "active"
        and a.get("labels", {}).get("namespace", "shop") == "shop"
    ]


def firing(alerts: list[dict], names: list[str], since: datetime | None = None) -> list[dict]:
    """The first alert for each name, if every name is firing (and started after `since`).
    Empty when any name is missing."""
    found = {}
    for a in alerts:
        name = a.get("labels", {}).get("alertname")
        started = _starts_at(a)
        if name not in names or name in found:
            continue
        if since is not None and (started is None or started < since):
            continue
        found[name] = a
    return [found[n] for n in names] if len(found) == len(names) else []


def alert_text(alerts: list[dict]) -> str:
    """What the agent reads, one line per alert, in the same shape as the catalog's alerts."""
    lines = []
    for a in alerts:
        name = a["labels"]["alertname"]
        desc = " ".join((a.get("annotations", {}).get("description") or "").split())
        lines.append(f"{name}: {desc}" if desc else name)
    return "\n".join(lines)


def wait_clear(k: Kube, names: list[str], timeout: float = CLEAR_TIMEOUT) -> float:
    """Wait until none of `names` is firing, so a leftover alert isn't mistaken for a new one."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if not any(a["labels"].get("alertname") in names for a in active_alerts(k)):
            return time.monotonic() - start
        time.sleep(5)
    raise AlertTimeout(f"alerts {', '.join(names)} still firing after {timeout:.0f}s")


def wait_firing(
    k: Kube, names: list[str], since: datetime, timeout: float = FIRE_TIMEOUT
) -> list[dict]:
    """Wait until every alert in `names` fires with a start time after `since`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = firing(active_alerts(k), names, since)
        if found:
            return found
        time.sleep(5)
    raise AlertTimeout(f"alerts {', '.join(names)} did not fire within {timeout:.0f}s")


def now() -> datetime:
    return datetime.now(UTC)
