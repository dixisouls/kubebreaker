"""Two-fault combinations. No cluster."""

from dataclasses import replace

import pytest

from kubebreaker import sweep
from kubebreaker.chaos import CATALOG, COMBOS, Tier, get_fault
from kubebreaker.chaos.combos import combine


def test_combos_are_separate_from_the_single_fault_catalog():
    assert COMBOS and not set(COMBOS) & set(CATALOG)
    assert sweep.resolve_faults("all") == list(CATALOG)
    assert sweep.resolve_faults(["multi"]) == list(COMBOS)


@pytest.mark.parametrize("fault_id", list(COMBOS))
def test_combo_merges_its_parts(fault_id):
    combo = COMBOS[fault_id]
    parts = [CATALOG[i] for i in fault_id.split("+")]
    assert combo.tier == Tier.MULTI and get_fault(fault_id) is combo
    for p in parts:
        assert p.ground_truth in combo.ground_truth
        assert set(p.fix_scope) <= set(combo.fix_scope)
        assert p.alert in combo.alert.splitlines()
    # The parts must change different objects, or one fix could undo the other
    scopes = [{s.split(":")[0] for s in p.fix_scope} for p in parts]
    assert not scopes[0] & scopes[1]


def test_combine_runs_every_part_in_order():
    calls = []

    def part(i):
        return replace(
            CATALOG["bad-command"],
            id=i,
            inject=lambda k: calls.append(("inject", i)),
            reference_fix=lambda k: calls.append(("fix", i)),
            symptom=lambda k: i == "a",
            extra_checks=lambda k: [f"{i} broken"],
            quality_checks=lambda snap: [f"{i} band-aid"],
        )

    combo = combine(part("a"), part("b"))
    combo.inject(None)
    combo.reference_fix(None)
    assert calls == [("inject", "a"), ("inject", "b"), ("fix", "a"), ("fix", "b")]
    assert combo.id == "a+b"
    assert not combo.symptom(None)  # every part's symptom must be visible
    assert combo.extra_checks(None) == ["a broken", "b broken"]
    assert combo.quality_checks({}) == ["a band-aid", "b band-aid"]
    # Identical alerts are sent once
    assert combo.alert == CATALOG["bad-command"].alert


def test_combine_needs_two_faults():
    with pytest.raises(ValueError):
        combine(CATALOG["bad-command"])


def test_multi_experiment_loads_and_estimates_from_hard_history():
    exp = sweep.load("experiments/multi.yaml")
    assert exp.fault_ids == list(COMBOS)
    history = sweep.History()
    history.by_key[("gpt-6.1-terra", Tier.HARD)] = {"input_tokens": 1.0}
    history.count[("gpt-6.1-terra", Tier.HARD)] = 3
    _, source = history.lookup("gpt-6.1-terra", Tier.MULTI)
    assert source.startswith("history (hard tier)")
