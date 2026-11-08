"""One benchmark episode: reset, inject, alert the agent, let it work, verify, record."""

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from kubebreaker.agent.identity import agent_kube
from kubebreaker.agent.loop import run_agent
from kubebreaker.agent.tools import AgentTools
from kubebreaker.agent.trace import Trace
from kubebreaker.chaos import Fault, runner
from kubebreaker.kube import Kube
from kubebreaker.llm import LLMClient
from kubebreaker.scoring.judge import judge_root_cause
from kubebreaker.scoring.score import score_fix
from kubebreaker.scoring.snapshot import take_snapshot

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"
# After the agent stops, give the cluster a little time to settle before judging
SETTLE_SECONDS = 90


@dataclass
class EpisodeResult:
    fault_id: str
    tier: str
    provider: str
    model: str
    resolved: bool
    declared: bool
    stop_reason: str
    steps: int
    turns: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    agent_seconds: float
    # Wall clock from alert to the health check passing; None if never resolved
    time_to_resolve_seconds: float | None
    root_cause: str
    fix_summary: str
    ground_truth: str
    remaining_problems: list[str] = field(default_factory=list)
    mutations: list[dict] = field(default_factory=list)
    # Scores
    fix_quality: float = 0.0
    fix_label: str = "unresolved"
    band_aids: list[str] = field(default_factory=list)
    collateral: list[str] = field(default_factory=list)
    fix_changes: list[str] = field(default_factory=list)
    disruptive_actions: list[str] = field(default_factory=list)
    root_cause_verdict: str = ""
    root_cause_score: float = 0.0
    root_cause_reason: str = ""
    judge: str = ""
    # True when the judge's provider also ran the agent: results may be biased
    judge_same_provider: bool = False
    # Turns produced by a provider-side fallback model; exclude such runs from per-model stats
    fallback_turns: int = 0
    served_by: list[str] = field(default_factory=list)
    error: str = ""
    run_dir: str = ""


def new_run_dir(fault_id: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return RUNS_DIR / f"{stamp}-{fault_id}"


def run_episode(
    admin: Kube,
    fault: Fault,
    llm: LLMClient,
    max_steps: int = 20,
    max_tokens: int = 600_000,
    run_dir: Path | None = None,
) -> EpisodeResult:
    run_dir = run_dir or new_run_dir(fault.id)
    trace = Trace(run_dir / "trace.jsonl")

    trace.log("reset")
    runner.reset_app(admin)
    baseline = take_snapshot(admin)
    symptom_s = runner.inject(admin, fault)
    trace.log("injected", fault=fault.id, symptom_seconds=round(symptom_s, 1))

    tools = AgentTools(agent_kube(admin))
    alert_at = time.monotonic()
    outcome = run_agent(llm, tools, fault.alert, trace, max_steps, max_tokens)

    remaining = runner.verify(admin, fault, timeout=SETTLE_SECONDS)
    resolved = not remaining
    ttr = round(time.monotonic() - alert_at, 1) if resolved else None
    trace.log("verified", resolved=resolved, remaining=remaining)

    mutations = [asdict(m) for m in tools.mutations]
    final = take_snapshot(admin)
    # Kept so the episode can be re-scored later without re-running it
    (run_dir / "snapshots.json").write_text(
        json.dumps({"baseline": baseline, "final": final}, indent=1, default=str)
    )
    fix = score_fix(fault, resolved, baseline, final, mutations)
    trace.log("fix_score", **asdict(fix))
    try:
        judgement = judge_root_cause(fault.ground_truth, outcome.root_cause, outcome.fix_summary)
    except Exception as exc:
        judgement = None
        trace.log("judge_error", error=f"{type(exc).__name__}: {exc}")
    if judgement:
        trace.log("root_cause_judged", **asdict(judgement))

    result = EpisodeResult(
        fault_id=fault.id,
        tier=str(fault.tier),
        provider=llm.provider,
        model=llm.model,
        resolved=resolved,
        declared=outcome.declared,
        stop_reason=outcome.stop_reason,
        steps=outcome.steps,
        turns=outcome.turns,
        input_tokens=outcome.usage.input_tokens,
        output_tokens=outcome.usage.output_tokens,
        cached_tokens=outcome.usage.cached_tokens,
        agent_seconds=round(outcome.seconds, 1),
        time_to_resolve_seconds=ttr,
        root_cause=outcome.root_cause,
        fix_summary=outcome.fix_summary,
        ground_truth=fault.ground_truth,
        remaining_problems=remaining,
        mutations=mutations,
        fix_quality=fix.quality,
        fix_label=fix.label,
        band_aids=fix.band_aids,
        collateral=fix.collateral,
        fix_changes=fix.fix_changes,
        disruptive_actions=fix.disruptive_actions,
        root_cause_verdict=judgement.verdict if judgement else "judge_error",
        root_cause_score=judgement.score if judgement else 0.0,
        root_cause_reason=judgement.reason if judgement else "",
        judge=judgement.judge if judgement else "",
        judge_same_provider=bool(judgement) and judgement.judge.split("/")[0] == llm.provider,
        fallback_turns=outcome.fallback_turns,
        served_by=outcome.served_by,
        error=outcome.error,
        run_dir=str(run_dir),
    )
    (run_dir / "result.json").write_text(json.dumps(asdict(result), indent=2))
    return result
