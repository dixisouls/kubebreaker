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


def inject(k: Kube, fault: Fault, timeout: float = SYMPTOM_TIMEOUT) -> float:
    """Inject the fault and wait until its symptom is visible. Returns seconds waited."""
    start = time.monotonic()
    fault.inject(k)
    if not wait_for(lambda: fault.symptom(k), timeout):
        raise ChaosError(f"{fault.id}: symptom did not appear within {timeout}s")
    return time.monotonic() - start


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
