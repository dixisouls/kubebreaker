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

RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"
# After the agent stops, give the cluster a little time to settle before judging
SETTLE_SECONDS = 60


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
    symptom_s = runner.inject(admin, fault)
    trace.log("injected", fault=fault.id, symptom_seconds=round(symptom_s, 1))

    tools = AgentTools(agent_kube(admin))
    alert_at = time.monotonic()
    outcome = run_agent(llm, tools, fault.alert, trace, max_steps, max_tokens)

    remaining = runner.verify(admin, fault, timeout=SETTLE_SECONDS)
    resolved = not remaining
    ttr = round(time.monotonic() - alert_at, 1) if resolved else None
    trace.log("verified", resolved=resolved, remaining=remaining)

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
        mutations=[asdict(m) for m in tools.mutations],
        error=outcome.error,
        run_dir=str(run_dir),
    )
    (run_dir / "result.json").write_text(json.dumps(asdict(result), indent=2))
    return result
