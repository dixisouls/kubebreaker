"""Incidents with two faults at once, built from catalog faults.

A combination injects every part, alerts with every distinct part alert, and is resolved only
when every part's checks pass. Its fix scope is the union of the parts' scopes, so fixing both
faults is never counted as collateral damage. The pairs are chosen so the parts touch
different objects, and so fixing the louder fault leaves the app still broken: the agent has
to notice it isn't done.

Combinations live in COMBOS, not CATALOG, so `faults: all` still means the 12 single faults.
Use `faults: [multi]` or combination ids in an experiment file.
"""

from kubebreaker.chaos.base import CATALOG, Fault, Tier
from kubebreaker.kube import Kube

COMBOS: dict[str, Fault] = {}


def combine(*parts: Fault) -> Fault:
    if len(parts) < 2:
        raise ValueError("a combination needs at least two faults")
    alerts = list(dict.fromkeys(p.alert for p in parts))

    def inject(k: Kube) -> None:
        for p in parts:
            p.inject(k)

    def reference_fix(k: Kube) -> None:
        for p in parts:
            p.reference_fix(k)

    numbered = "\n".join(f"{i}. {p.ground_truth}" for i, p in enumerate(parts, 1))
    return Fault(
        id="+".join(p.id for p in parts),
        tier=Tier.MULTI,
        title=" and ".join(p.title for p in parts),
        alert="\n".join(alerts),
        ground_truth=(
            f"{len(parts)} independent faults were present at the same time. A correct "
            f"root cause names all of them; naming only some is partial.\n{numbered}"
        ),
        expected_fix="\n".join(f"{i}. {p.expected_fix}" for i, p in enumerate(parts, 1)),
        inject=inject,
        symptom=lambda k: all(p.symptom(k) for p in parts),
        reference_fix=reference_fix,
        extra_checks=lambda k: [c for p in parts for c in p.extra_checks(k)],
        fix_scope=tuple(dict.fromkeys(s for p in parts for s in p.fix_scope)),
        allowed_restarts=tuple(dict.fromkeys(r for p in parts for r in p.allowed_restarts)),
        quality_checks=lambda snap: [q for p in parts for q in p.quality_checks(snap)],
        collateral_checks=lambda final, base: [
            c for p in parts for c in p.collateral_checks(final, base)
        ],
    )


def register_combo(*ids: str) -> Fault:
    fault = combine(*(CATALOG[i] for i in ids))
    if fault.id in COMBOS or fault.id in CATALOG:
        raise ValueError(f"duplicate fault id {fault.id!r}")
    COMBOS[fault.id] = fault
    return fault


# api scaled to zero hides the orders database misconfiguration behind it
register_combo("scaled-to-zero", "wrong-db-host")
# orders crash-loops on OOM; once it's up, a NetworkPolicy still cuts the api off from it
register_combo("oom-killed", "netpol-block")
# a stuck frontend rollout (old pods keep serving) next to a Redis that rejects writes
register_combo("bad-image-tag", "redis-oom")
# a stuck orders rollout (old pods keep serving) next to a CPU-starved Redis: one alert is
# about a rollout, the other about latency, and neither explains the other
register_combo("missing-secret-ref", "cpu-starvation")


def get_fault(fault_id: str) -> Fault:
    """A catalog fault, a combination or a generated fault, by id. KeyError if unknown."""
    if fault_id in CATALOG:
        return CATALOG[fault_id]
    if fault_id in COMBOS:
        return COMBOS[fault_id]
    from kubebreaker.chaos import generated

    return generated.load_all()[fault_id][1]
