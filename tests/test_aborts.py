"""API failures abort an episode instead of being recorded as agent failures. No cluster."""

import json
from types import SimpleNamespace as NS

import pytest

from kubebreaker import episode, sweep
from kubebreaker.agent.loop import AgentOutcome
from kubebreaker.chaos import CATALOG
from kubebreaker.llm import Usage


@pytest.fixture
def fake_cluster(monkeypatch):
    """Replace every cluster touchpoint of run_episode with no-ops."""
    monkeypatch.setattr(episode.runner, "reset_app", lambda k: None)
    monkeypatch.setattr(episode.runner, "inject", lambda k, f, hold=0: 1.0)
    monkeypatch.setattr(episode.runner, "verify", lambda k, f, timeout: [])
    monkeypatch.setattr(episode, "take_snapshot", lambda k: {})
    monkeypatch.setattr(episode, "agent_kube", lambda k: None)
    monkeypatch.setattr(episode, "AgentTools", lambda k: NS(mutations=[]))


def outcome(**kw):
    base = dict(
        declared=True,
        root_cause="rc",
        fix_summary="fix",
        steps=3,
        turns=2,
        usage=Usage(1_000_000, 0),
        stop_reason="declared",
    )
    return AgentOutcome(**{**base, **kw})


LLM = NS(provider="openai", model="gpt-6.1-terra")
FAULT = CATALOG["scaled-to-zero"]


def test_agent_api_error_aborts_without_a_result(tmp_path, fake_cluster, monkeypatch):
    monkeypatch.setattr(
        episode,
        "run_agent",
        lambda *a: outcome(
            stop_reason="llm_error", error="PermissionDeniedError: credit balance too low"
        ),
    )
    with pytest.raises(episode.EpisodeAborted) as exc:
        episode.run_episode(None, FAULT, LLM, run_dir=tmp_path)
    assert exc.value.cost_usd == pytest.approx(1.0)  # 1M input tokens of terra
    assert not (tmp_path / "result.json").exists()
    record = json.loads((tmp_path / "errors.jsonl").read_text())
    assert record["stage"] == "agent" and "credit" in record["error"]


def test_judge_api_error_aborts_without_a_result(tmp_path, fake_cluster, monkeypatch):
    monkeypatch.setattr(episode, "run_agent", lambda *a: outcome())

    def broken_judge(*a):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(episode, "judge_root_cause", broken_judge)
    with pytest.raises(episode.EpisodeAborted):
        episode.run_episode(None, FAULT, LLM, run_dir=tmp_path)
    assert not (tmp_path / "result.json").exists()
    assert json.loads((tmp_path / "errors.jsonl").read_text())["stage"] == "judge"


def test_retry_keeps_the_failed_attempts_trace(tmp_path, fake_cluster, monkeypatch):
    (tmp_path / "trace.jsonl").write_text('{"kind": "old"}\n')
    monkeypatch.setattr(episode, "run_agent", lambda *a: outcome(stop_reason="llm_error"))
    with pytest.raises(episode.EpisodeAborted):
        episode.run_episode(None, FAULT, LLM, run_dir=tmp_path)
    assert "old" in (tmp_path / "trace.attempt-1.jsonl").read_text()


def test_spend_counts_aborted_attempts(tmp_path):
    (tmp_path / "result.json").write_text(
        json.dumps({"agent_cost_usd": 0.05, "judge_cost_usd": 0.01})
    )
    (tmp_path / "errors.jsonl").write_text('{"cost_usd": 0.02}\n{"cost_usd": 0.03}\n')
    assert sweep._spent(tmp_path) == pytest.approx(0.11)


def test_sweep_skips_a_provider_after_repeated_api_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "RUNS_DIR", tmp_path)
    exp = sweep.Experiment(
        "t",
        1,
        ["scaled-to-zero", "bad-command", "wrong-db-host", "oom-killed", "broken-selector"],
        [sweep.Config("a", "anthropic", "claude-sonnet-5-5")],
    )
    calls = []

    def always_aborts(admin, fault, llm, max_steps, run_dir):
        calls.append(fault.id)
        raise episode.EpisodeAborted("no credits", 0.0)

    monkeypatch.setattr(episode, "run_episode", always_aborts)
    monkeypatch.setattr("kubebreaker.kube.Kube.from_env", staticmethod(lambda: None))
    monkeypatch.setattr("kubebreaker.llm.make_client", lambda *a, **k: None)
    sweep.run_sweep(exp, log=lambda *a: None)
    assert len(calls) == sweep.MAX_CONSECUTIVE_API_FAILURES
    assert len(exp.pending()) == 5  # nothing recorded, all retried next run


def test_sustained_needs_an_unbroken_run():
    from kubebreaker.chaos.runner import sustained

    flaky = iter([True, True, False, True, True, True, True, True])
    assert sustained(lambda: next(flaky), seconds=0.12, timeout=2, interval=0.05)

    always_off = sustained(lambda: False, seconds=0.05, timeout=0.3, interval=0.05)
    assert always_off is False


def test_sustained_treats_errors_as_a_break():
    from kubebreaker.chaos.runner import sustained

    def boom():
        raise RuntimeError("api hiccup")

    assert sustained(boom, seconds=0.05, timeout=0.3, interval=0.05) is False


def test_episode_records_the_alert_hold(tmp_path, fake_cluster, monkeypatch):
    from kubebreaker.scoring.judge import Judgement
    from kubebreaker.scoring.score import FixScore

    monkeypatch.setenv("ALERT_HOLD_SECONDS", "45")
    seen = {}

    def fake_inject(k, f, hold=0):
        seen["hold"] = hold
        return 1.0

    monkeypatch.setattr(episode.runner, "inject", fake_inject)
    monkeypatch.setattr(episode, "run_agent", lambda *a: outcome())
    monkeypatch.setattr(
        episode,
        "judge_root_cause",
        lambda *a: Judgement("correct", 1.0, "", "google/gemini-3.8-flash"),
    )
    monkeypatch.setattr(episode, "score_fix", lambda *a: FixScore(1.0, "clean"))
    r = episode.run_episode(None, FAULT, LLM, run_dir=tmp_path)
    assert seen["hold"] == 45 and r.alert_hold_seconds == 45


def test_zero_hold_needs_one_passing_poll_and_negative_hold_is_rejected(monkeypatch):
    from kubebreaker.chaos import runner

    assert runner.sustained(lambda: True, seconds=0, timeout=1, interval=0.05)
    monkeypatch.setenv("ALERT_HOLD_SECONDS", "-5")
    with pytest.raises(ValueError):
        runner.alert_hold_seconds()
    monkeypatch.setenv("ALERT_HOLD_SECONDS", "abc")
    with pytest.raises(ValueError, match="whole number"):
        runner.alert_hold_seconds()


def test_sustained_does_not_overshoot_its_timeout():
    import time

    from kubebreaker.chaos.runner import sustained

    start = time.monotonic()
    assert sustained(lambda: False, seconds=1, timeout=0.2, interval=0.5) is False
    assert time.monotonic() - start < 0.45  # one 0.5s sleep would overshoot


def test_time_estimate_follows_the_configured_hold(monkeypatch):
    def secs(hold):
        monkeypatch.setenv("ALERT_HOLD_SECONDS", str(hold))
        job = sweep.Job(sweep.Config("x", "openai", "gpt-6.1-terra"), "wrong-db-host", 1)
        return sweep.estimate_job(job, sweep.History()).seconds

    assert secs(90) - secs(0) == pytest.approx(90)
