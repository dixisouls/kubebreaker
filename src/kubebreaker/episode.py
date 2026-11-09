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
from kubebreaker.pricing import cost_usd
from kubebreaker.scoring.judge import judge_root_cause
from kubebreaker.scoring.score import score_fix
from kubebreaker.scoring.snapshot import take_snapshot

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"
ERRORS_FILE = "errors.jsonl"
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
    cache_write_tokens: int
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
    # Dollar cost from pricing.PRICES; None when a model has no price entry
    agent_cost_usd: float | None = None
    judge_cost_usd: float | None = None
    max_steps: int = 0
    effort: str = ""
    # Seconds the symptom had to persist before the agent was alerted (0 in runs before this
    # setting existed); compare only runs with the same value
    alert_hold_seconds: int = 0
    error: str = ""
    run_dir: str = ""


class EpisodeAborted(RuntimeError):
    """The episode couldn't be measured because an API call failed (no credits, rate limits,
    outages). It isn't a result: nothing is recorded as a failure, and a sweep retries it.
    Money already spent on the attempt is in `cost_usd` and in errors.jsonl."""

    def __init__(self, message: str, cost_usd: float):
        super().__init__(message)
        self.cost_usd = cost_usd


def _abort(run_dir: Path, trace: Trace, stage: str, error: str, cost: float) -> EpisodeAborted:
    record = {
        "time": datetime.now(UTC).isoformat(),
        "stage": stage,
        "error": error,
        "cost_usd": round(cost, 6),
    }
    trace.log("aborted", **record)
    with (run_dir / ERRORS_FILE).open("a") as f:
        f.write(json.dumps(record) + "\n")
    return EpisodeAborted(f"{stage} API call failed: {error}", cost)


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
    trace_path = run_dir / "trace.jsonl"
    if trace_path.exists():
        # A retry after an aborted attempt: keep the old trace for inspection
        n = len(list(run_dir.glob("trace.attempt-*.jsonl"))) + 1
        trace_path.rename(run_dir / f"trace.attempt-{n}.jsonl")
    trace = Trace(trace_path)

    trace.log("reset")
    runner.reset_app(admin)
    baseline = take_snapshot(admin)
    hold = runner.alert_hold_seconds()
    symptom_s = runner.inject(admin, fault, hold=hold)
    trace.log("injected", fault=fault.id, symptom_seconds=round(symptom_s, 1), alert_hold=hold)

    tools = AgentTools(agent_kube(admin))
    alert_at = time.monotonic()
    outcome = run_agent(llm, tools, fault.alert, trace, max_steps, max_tokens)
    agent_cost = cost_usd(llm.model, outcome.usage) or 0.0
    if outcome.stop_reason == "llm_error":
        raise _abort(run_dir, trace, "agent", outcome.error, agent_cost)

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
        raise _abort(run_dir, trace, "judge", f"{type(exc).__name__}: {exc}", agent_cost) from exc
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
        cache_write_tokens=outcome.usage.cache_write_tokens,
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
        root_cause_verdict=judgement.verdict,
        root_cause_score=judgement.score,
        root_cause_reason=judgement.reason,
        judge=judgement.judge,
        judge_same_provider=judgement.judge.split("/")[0] == llm.provider,
        agent_cost_usd=cost_usd(llm.model, outcome.usage),
        judge_cost_usd=(
            cost_usd(judgement.judge.split("/", 1)[1], judgement.usage) if judgement.usage else 0.0
        ),
        max_steps=max_steps,
        alert_hold_seconds=hold,
        effort=getattr(llm, "effort", None) or getattr(llm, "reasoning_effort", None) or "",
        fallback_turns=outcome.fallback_turns,
        served_by=outcome.served_by,
        error=outcome.error,
        run_dir=str(run_dir),
    )
    (run_dir / "result.json").write_text(json.dumps(asdict(result), indent=2))
    return result
