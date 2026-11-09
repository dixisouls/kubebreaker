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
    monkeypatch.setattr(episode.runner, "inject", lambda k, f: 1.0)
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
