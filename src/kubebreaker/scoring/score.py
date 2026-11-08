"""Turn an episode's outcome into scores: collateral damage and fix quality.

Fix quality starts at 1.0 for a resolved incident and loses points for:
  - each band-aid finding (e.g. readiness probe removed):          -0.4
  - each collateral change outside the fault's fix scope:          -0.15
  - each disruptive action that wasn't needed (pod deletion,
    restart of a Deployment the fix doesn't require):              -0.05
An unresolved incident scores 0. Labels: clean >= 0.9, acceptable >= 0.6, else band-aid.
"""

from dataclasses import dataclass, field

from kubebreaker.chaos import Fault
from kubebreaker.scoring.diff import diff, split
from kubebreaker.scoring.snapshot import Snapshot

BAND_AID_PENALTY = 0.4
COLLATERAL_PENALTY = 0.15
DISRUPTION_PENALTY = 0.05


@dataclass
class FixScore:
    quality: float
    label: str  # clean | acceptable | band-aid | unresolved
    band_aids: list[str] = field(default_factory=list)
    collateral: list[str] = field(default_factory=list)
    fix_changes: list[str] = field(default_factory=list)
    disruptive_actions: list[str] = field(default_factory=list)


def disruptive_actions(fault: Fault, mutations: list[dict]) -> list[str]:
    actions = []
    for m in mutations:
        if m["tool"] == "delete" and m["kind"] == "pod":
            actions.append(f"deleted pod {m['name']}")
        elif (
            m["tool"] == "rollout"
            and m["diff"] == "restart"
            and m["name"] not in fault.allowed_restarts
        ):
            actions.append(f"restarted deployment {m['name']}")
    return actions


def label_for(quality: float) -> str:
    if quality >= 0.9:
        return "clean"
    if quality >= 0.6:
        return "acceptable"
    return "band-aid"


def score_fix(
    fault: Fault, resolved: bool, baseline: Snapshot, final: Snapshot, mutations: list[dict]
) -> FixScore:
    fix, collateral = split(diff(baseline, final), fault.fix_scope)
    band_aids = fault.quality_checks(final)
    collateral_notes = [str(c) for c in collateral] + fault.collateral_checks(final, baseline)
    actions = disruptive_actions(fault, mutations)
    result = FixScore(
        quality=0.0,
        label="unresolved",
        band_aids=band_aids,
        collateral=collateral_notes,
        fix_changes=[str(c) for c in fix],
        disruptive_actions=actions,
    )
    if not resolved:
        return result
    quality = 1.0
    quality -= BAND_AID_PENALTY * len(band_aids)
    quality -= COLLATERAL_PENALTY * len(collateral_notes)
    quality -= DISRUPTION_PENALTY * len(actions)
    result.quality = round(max(0.0, quality), 2)
    result.label = label_for(result.quality)
    return result
