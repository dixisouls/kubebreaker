"""Sweep planning and cost estimation. No cluster or API calls."""

import json

import pytest

from kubebreaker import sweep
from kubebreaker.chaos import CATALOG
from kubebreaker.llm import Usage
from kubebreaker.pricing import cost_usd


def write_exp(tmp_path, body: str):
    p = tmp_path / "exp.yaml"
    p.write_text(body)
    return sweep.load(p)


EXP = """
name: t
repeats: 2
faults: [hard, wrong-db-host]
configs:
  - {label: a, provider: openai, model: gpt-6.1-terra}
  - {label: b, provider: anthropic, model: claude-sonnet-5-5, effort: high, max_steps: 10}
"""


def test_load_resolves_tiers_and_ids(tmp_path):
    exp = write_exp(tmp_path, EXP)
    hard = [f.id for f in CATALOG.values() if f.tier == "hard"]
    assert exp.fault_ids == [*hard, "wrong-db-host"]
    assert exp.configs[1].max_steps == 10 and exp.configs[1].effort == "high"


def test_jobs_are_repeat_major(tmp_path):
    exp = write_exp(tmp_path, EXP)
    jobs = exp.jobs()
    assert len(jobs) == 2 * 2 * 5
    half = len(jobs) // 2
    assert {j.repeat for j in jobs[:half]} == {1} and {j.repeat for j in jobs[half:]} == {2}


def test_unknown_fault_and_duplicate_labels_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_exp(
            tmp_path, "name: t\nfaults: [nope]\nconfigs: [{label: a, provider: openai, model: m}]"
        )
    with pytest.raises(ValueError):
        write_exp(
            tmp_path,
            "name: t\nconfigs: [{label: a, provider: openai, model: m}, "
            "{label: a, provider: openai, model: m}]",
        )


def test_finished_jobs_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "RUNS_DIR", tmp_path / "runs")
    exp = write_exp(tmp_path, EXP)
    first = exp.jobs()[0]
    d = first.run_dir(exp.name)
    d.mkdir(parents=True)
    (d / "result.json").write_text("{}")
    assert first not in exp.pending() and len(exp.pending()) == len(exp.jobs()) - 1


def test_cost_uses_cached_and_cache_write_rates():
    # 1M uncached input + 1M cached + 1M written + 1M output on Sonnet 5.5
    u = Usage(
        input_tokens=3_000_000,
        output_tokens=1_000_000,
        cached_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost_usd("claude-sonnet-5-5", u) == pytest.approx(2.00 + 0.20 + 2.50 + 10.00)
    assert cost_usd("gpt-6.1-terra", Usage(1_000_000, 1_000_000, 0)) == pytest.approx(6.00)
    assert cost_usd("unknown-model", u) is None


def test_estimate_scales_with_steps_and_effort(tmp_path):
    runs = tmp_path / "runs"
    for i, steps in enumerate([20, 20]):
        d = runs / f"r{i}"
        d.mkdir(parents=True)
        d.joinpath("result.json").write_text(
            json.dumps(
                {
                    "model": "gpt-6.1-terra",
                    "tier": "medium",
                    "steps": steps,
                    "input_tokens": 40_000,
                    "cached_tokens": 30_000,
                    "output_tokens": 1_000,
                    "agent_seconds": 30,
                }
            )
        )
    history = sweep.History.from_runs(runs)
    cfg = sweep.Config("x", "openai", "gpt-6.1-terra")

    def est(**kw):
        c = sweep.Config(**{**vars(cfg), **kw})
        return sweep.estimate_job(sweep.Job(c, "wrong-db-host", 1), history)

    base = est()
    assert est(max_steps=10).cost_usd < base.cost_usd < est(max_steps=30).cost_usd
    assert est(effort="high").cost_usd > base.cost_usd
    assert "n=2" in base.source
