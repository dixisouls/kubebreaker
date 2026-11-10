"""The Fault definition and the catalog registry."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from kubebreaker.chaos.checks import app_problems
from kubebreaker.kube import Kube


class Tier(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    # Two faults at once (chaos/combos.py)
    MULTI = "multi"


@dataclass(frozen=True)
class Fault:
    id: str
    tier: Tier
    title: str
    # What the agent receives. Describes the symptom from the outside, never the cause.
    alert: str
    # The real cause, used to grade the agent's root-cause write-up
    ground_truth: str
    # What a correct fix looks like, used to grade fix quality
    expected_fix: str
    inject: Callable[[Kube], None]
    # True once the fault is visible in the cluster
    symptom: Callable[[Kube], bool]
    # A known-good fix. Used only by selftest to prove the fault can be fixed.
    reference_fix: Callable[[Kube], None]
    # Extra fault-specific checks on top of the general app health check
    extra_checks: Callable[[Kube], list[str]] = field(default=lambda k: [])

    # --- scoring ---
    # Where the fix belongs, as "kind/name:path.prefix" in snapshot paths. Changes inside the
    # scope are the fix; changes anywhere else count as collateral damage.
    fix_scope: tuple[str, ...] = ()
    # Deployments where a restart is part of a correct fix (not a band-aid)
    allowed_restarts: tuple[str, ...] = ()
    # Inspect the final snapshot and return band-aid findings (e.g. "readiness probe removed")
    quality_checks: Callable[[dict], list[str]] = field(default=lambda snap: [])
    # Compare final and baseline snapshots for collateral changes that fix_scope can't express,
    # e.g. extra edits inside the same file the fix belongs in
    collateral_checks: Callable[[dict, dict], list[str]] = field(default=lambda final, base: [])

    def problems(self, k: Kube) -> list[str]:
        """Empty when the app is back to baseline health and the fault is gone."""
        return app_problems(k) + self.extra_checks(k)


CATALOG: dict[str, Fault] = {}


def register(fault: Fault) -> Fault:
    if fault.id in CATALOG:
        raise ValueError(f"duplicate fault id {fault.id!r}")
    CATALOG[fault.id] = fault
    return fault
