"""Run faults: reset the app, inject, wait for the symptom, verify recovery."""

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from kubebreaker.chaos.base import Fault
from kubebreaker.kube import Kube, wait_for

REPO_ROOT = Path(__file__).resolve().parents[3]
SYMPTOM_TIMEOUT = 120
VERIFY_TIMEOUT = 150
# Like an alerting rule's `for:` clause: the symptom must hold this long before the agent is
# paged. It also gives Prometheus time to collect a few samples, so metrics reflect the fault.
DEFAULT_ALERT_HOLD = 45


def alert_hold_seconds() -> int:
    raw = os.environ.get("ALERT_HOLD_SECONDS", str(DEFAULT_ALERT_HOLD))
    try:
        hold = int(raw)
    except ValueError:
        raise ValueError(f"ALERT_HOLD_SECONDS must be a whole number, got {raw!r}") from None
    if hold < 0:
        raise ValueError(f"ALERT_HOLD_SECONDS must be 0 or more, got {hold}")
    return hold


class ChaosError(RuntimeError):
    pass


def reset_app(k: Kube) -> None:
    """Restore the shop app to its baseline and confirm it's healthy."""
    result = subprocess.run(
        [REPO_ROOT / "scripts" / "reset-app.sh"],
        env={**os.environ, "KUBE_CONTEXT": k.context},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ChaosError(f"reset failed:\n{result.stdout}{result.stderr}")


def inject(k: Kube, fault: Fault, timeout: float = SYMPTOM_TIMEOUT, hold: float = 0) -> float:
    """Inject the fault and wait until its symptom is visible, then until it has held for
    `hold` seconds without a break. Returns seconds waited in total."""
    start = time.monotonic()
    fault.inject(k)
    if not wait_for(lambda: fault.symptom(k), timeout):
        raise ChaosError(f"{fault.id}: symptom did not appear within {timeout}s")
    # The hold gets its own window: a symptom that flickers at first (e.g. while the Redis
    # cache drains) may need a while to settle into an unbroken run of `hold` seconds. Worst
    # case, inject takes timeout + hold + timeout.
    if hold > 0 and not sustained(lambda: fault.symptom(k), hold, timeout=hold + timeout):
        raise ChaosError(f"{fault.id}: symptom didn't hold for {hold}s in a row")
    return time.monotonic() - start


def sustained(check, seconds: float, timeout: float, interval: float = 5.0) -> bool:
    """True once `check` has been true on every poll for `seconds`; a false (or erroring)
    poll restarts the clock. False if that doesn't happen within `timeout`.

    With `seconds` <= 0, one passing poll is enough (there's no duration to hold for).
    """
    deadline = time.monotonic() + timeout
    since = time.monotonic()
    while time.monotonic() < deadline:
        try:
            ok = check()
        except Exception:
            ok = False
        now = time.monotonic()
        if not ok:
            since = now
        elif now - since >= seconds:
            return True
        # Never sleep past the deadline
        time.sleep(max(0.0, min(interval, deadline - time.monotonic())))
    return False


def verify(k: Kube, fault: Fault, timeout: float = VERIFY_TIMEOUT) -> list[str]:
    """Wait for the app to recover. Returns the remaining problems (empty means fixed)."""
    problems: list[str] = []

    def healthy() -> bool:
        nonlocal problems
        problems = fault.problems(k)
        return not problems

    wait_for(healthy, timeout, interval=3)
    return problems


@dataclass
class SelftestResult:
    fault_id: str
    ok: bool
    symptom_seconds: float = 0.0
    recover_seconds: float = 0.0
    error: str = ""
    problems: list[str] = field(default_factory=list)


def selftest(k: Kube, fault: Fault) -> SelftestResult:
    """Prove a fault works end to end: reset, inject, see symptom, apply the reference fix,
    verify recovery. The app must also be broken (problems non-empty) while the fault is live."""
    try:
        reset_app(k)
        symptom_s = inject(k, fault)
        if not fault.problems(k):
            error = "fault is live but the app looks healthy"
            return SelftestResult(fault.id, False, symptom_s, error=error)
        start = time.monotonic()
        fault.reference_fix(k)
        remaining = verify(k, fault)
        recover_s = time.monotonic() - start
        if remaining:
            return SelftestResult(fault.id, False, symptom_s, recover_s, "not fixed", remaining)
        return SelftestResult(fault.id, True, symptom_s, recover_s)
    except Exception as exc:
        return SelftestResult(fault.id, False, error=f"{type(exc).__name__}: {exc}")
