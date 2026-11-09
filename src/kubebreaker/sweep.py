"""Experiment sweeps: run every (config, fault, repeat) in an experiment file, resumably.

An experiment file (YAML) looks like:

    name: core
    repeats: 3
    faults: all            # or a list of fault ids and/or tiers, e.g. [hard, wrong-db-host]
    configs:
      - label: terra
        provider: openai
        model: gpt-6.1-terra
        max_steps: 20      # optional, default 20
        effort: ""         # optional reasoning effort
      - label: sonnet
        provider: anthropic
        model: claude-sonnet-5-5
        effort: medium

Results land in runs/sweeps/<name>/<label>/<fault>/r<n>/. A job whose result.json exists is
skipped, so an interrupted sweep picks up where it stopped.
"""

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from kubebreaker.chaos import CATALOG, Tier
from kubebreaker.episode import RUNS_DIR
from kubebreaker.llm import Usage
from kubebreaker.pricing import PRICES, cost_usd

DEFAULT_STEPS = 20
# Fixed per-episode overhead beyond the agent itself: reset (~20s), waiting for the
# symptom (5-60s), the alert hold, and verification/settling (up to 90s when the fix
# didn't land)
OVERHEAD_SECONDS = 75 + 45
# Rough output-token multipliers by reasoning effort, relative to the default
EFFORT_OUTPUT = {"": 1.0, "low": 0.6, "medium": 1.0, "high": 1.6, "xhigh": 2.5, "max": 3.5}
# Used when there's no history at all for a model
FALLBACK = {
    "input": 45_000,
    "cached": 36_000,
    "cache_write": 0,
    "output": 1_500,
    "steps": 18.0,
    "agent_seconds": 40.0,
}


@dataclass(frozen=True)
class Config:
    label: str
    provider: str
    model: str
    max_steps: int = DEFAULT_STEPS
    effort: str = ""


@dataclass(frozen=True)
class Job:
    config: Config
    fault_id: str
    repeat: int

    def run_dir(self, sweep: str) -> Path:
        return RUNS_DIR / "sweeps" / sweep / self.config.label / self.fault_id / f"r{self.repeat}"


@dataclass
class Experiment:
    name: str
    repeats: int
    fault_ids: list[str]
    configs: list[Config]

    def jobs(self) -> list[Job]:
        # Repeat-major order: one full pass over everything before the second repeat, so a
        # stopped sweep still has balanced coverage
        return [
            Job(c, f, r)
            for r in range(1, self.repeats + 1)
            for c in self.configs
            for f in self.fault_ids
        ]

    def pending(self) -> list[Job]:
        return [j for j in self.jobs() if not (j.run_dir(self.name) / "result.json").exists()]


def resolve_faults(spec) -> list[str]:
    if spec in (None, "all"):
        return list(CATALOG)
    ids: list[str] = []
    tiers = {t.value for t in Tier}
    for item in spec:
        if item in tiers:
            ids += [f.id for f in CATALOG.values() if f.tier == item]
        elif item in CATALOG:
            ids.append(item)
        else:
            raise ValueError(f"unknown fault or tier {item!r}")
    return list(dict.fromkeys(ids))


def load(path: str | Path) -> Experiment:
    data = yaml.safe_load(Path(path).read_text())
    configs = [Config(**c) for c in data["configs"]]
    labels = [c.label for c in configs]
    if len(set(labels)) != len(labels):
        raise ValueError("config labels must be unique")
    return Experiment(
        name=data["name"],
        repeats=int(data.get("repeats", 1)),
        fault_ids=resolve_faults(data.get("faults", "all")),
        configs=configs,
    )


# --- estimating ---


@dataclass
class History:
    """Average per-episode usage by (model, tier), from every result.json on disk."""

    by_key: dict[tuple[str, str], dict[str, float]] = field(default_factory=dict)
    count: dict[tuple[str, str], int] = field(default_factory=dict)

    @classmethod
    def from_runs(cls, root: Path = RUNS_DIR) -> "History":
        groups: dict[tuple[str, str], list[dict]] = {}
        for f in root.glob("**/result.json"):
            r = json.loads(f.read_text())
            if not r.get("steps"):
                continue  # refused on turn one, or never started
            for key in ((r["model"], r["tier"]), (r["model"], "*")):
                groups.setdefault(key, []).append(r)
        h = cls()
        for key, rows in groups.items():
            h.count[key] = len(rows)
            h.by_key[key] = {
                "input": statistics.mean(r["input_tokens"] for r in rows),
                "cached": statistics.mean(r["cached_tokens"] for r in rows),
                "cache_write": statistics.mean(r.get("cache_write_tokens", 0) for r in rows),
                "output": statistics.mean(r["output_tokens"] for r in rows),
                "steps": statistics.mean(r["steps"] for r in rows),
                "agent_seconds": statistics.mean(r["agent_seconds"] for r in rows),
            }
        return h

    def lookup(self, model: str, tier: str) -> tuple[dict[str, float], str]:
        for key, source in (((model, tier), "history"), ((model, "*"), "history (all tiers)")):
            if key in self.by_key:
                return self.by_key[key], f"{source}, n={self.count[key]}"
        return FALLBACK, "no history: generic guess"


@dataclass
class Estimate:
    cost_usd: float
    seconds: float
    priced: bool
    source: str


def estimate_job(job: Job, history: History) -> Estimate:
    c = job.config
    base, source = history.lookup(c.model, CATALOG[job.fault_id].tier)
    # Episodes use about `steps` tool calls at a 20-call cap. Input grows faster than linearly
    # with steps (each turn re-sends the history), output roughly linearly.
    if c.max_steps < base["steps"]:
        steps = c.max_steps  # the cap cuts the episode short
    elif c.max_steps > DEFAULT_STEPS:
        steps = base["steps"] * c.max_steps / DEFAULT_STEPS  # upper bound: the extra room gets used
    else:
        steps = base["steps"]
    scale = steps / base["steps"] if base["steps"] else 1.0
    effort = EFFORT_OUTPUT.get(c.effort, 1.0)
    usage = Usage(
        input_tokens=int(base["input"] * scale**1.5),
        output_tokens=int(base["output"] * scale * effort),
        cached_tokens=int(base["cached"] * scale**1.5),
        cache_write_tokens=int(base["cache_write"] * scale),
    )
    cost = cost_usd(c.model, usage)
    seconds = OVERHEAD_SECONDS + base["agent_seconds"] * scale * (0.5 + effort / 2)
    return Estimate(cost or 0.0, seconds, cost is not None, source)


def summarize_plan(exp: Experiment, jobs: list[Job], history: History) -> tuple[str, float]:
    """A printable plan with cost and time per config, and the total estimated cost."""
    lines = [f"experiment: {exp.name}  ({len(exp.fault_ids)} faults x {exp.repeats} repeats)"]
    header = (
        f"  {'config':<14}{'model':<20}{'steps':>6}{'effort':>8}{'jobs':>6}"
        f"{'est. cost':>11}{'est. time':>11}  basis"
    )
    lines.append(header)
    total_cost = total_secs = 0.0
    unpriced = set()
    for c in exp.configs:
        mine = [j for j in jobs if j.config == c]
        if not mine:
            continue
        ests = [estimate_job(j, history) for j in mine]
        cost = sum(e.cost_usd for e in ests)
        secs = sum(e.seconds for e in ests)
        total_cost += cost
        total_secs += secs
        if not all(e.priced for e in ests):
            unpriced.add(c.model)
        lines.append(
            f"  {c.label:<14}{c.model:<20}{c.max_steps:>6}{c.effort or '-':>8}{len(mine):>6}"
            f"{'$' + format(cost, '.2f'):>11}{secs / 3600:>10.1f}h  {_basis(history, c.model)}"
        )
    judge = len(jobs) * judge_cost_estimate()
    lines.append(
        f"  {'judge':<14}{'':<20}{'':>6}{'':>8}{len(jobs):>6}{'$' + format(judge, '.2f'):>11}"
    )
    total_cost += judge
    lines.append(
        f"  total: {len(jobs)} episodes, about ${total_cost:.2f} "
        f"(plan for up to ${total_cost * 2:.2f}), about {total_secs / 3600:.1f}h of cluster time"
    )
    if unpriced:
        lines.append(f"  warning: no price for {sorted(unpriced)}; add them to pricing.PRICES")
    missing = {c.model for c in exp.configs} - set(PRICES)
    if missing - unpriced:
        lines.append(f"  warning: no price for {sorted(missing - unpriced)}")
    return "\n".join(lines), total_cost


# One judgement, measured on gemini-3.8-flash at medium thinking: ~250 prompt tokens and
# ~125-200 output tokens including thinking. Rounded up for longer write-ups.
JUDGE_USAGE = Usage(input_tokens=400, output_tokens=300)


def judge_cost_estimate() -> float:
    import os

    model = os.environ.get("JUDGE_MODEL") or ""
    return cost_usd(model, JUDGE_USAGE) or 0.003


def _basis(history: History, model: str) -> str:
    n = history.count.get((model, "*"), 0)
    return f"{n} past episodes" if n else "no history: generic guess"


# --- running ---


# After this many API failures in a row, a provider is skipped for the rest of the run
# (usually means its credits ran out); its jobs stay pending for the next run
MAX_CONSECUTIVE_API_FAILURES = 3


def run_sweep(exp: Experiment, max_cost: float | None = None, log=print) -> list[dict]:
    """Run all pending jobs in order. Stops early once actual spend passes `max_cost`.
    Episodes that fail on an API error aren't recorded as results; they're retried next run."""
    from kubebreaker.episode import EpisodeAborted, run_episode
    from kubebreaker.kube import Kube
    from kubebreaker.llm import make_client

    admin = Kube.from_env()
    jobs = exp.pending()
    spent = sum(_spent(j.run_dir(exp.name)) for j in exp.jobs())
    failures: dict[str, int] = {}
    results = []
    for i, job in enumerate(jobs, 1):
        if max_cost is not None and spent >= max_cost:
            log(f"stopping: spent ${spent:.2f}, cap is ${max_cost:.2f}")
            break
        c = job.config
        if failures.get(c.provider, 0) >= MAX_CONSECUTIVE_API_FAILURES:
            continue
        run_dir = job.run_dir(exp.name)
        log(f"[{i}/{len(jobs)}] {c.label} {job.fault_id} r{job.repeat} ...")
        try:
            llm = make_client(c.provider, c.model, effort=c.effort or None)
            r = run_episode(admin, CATALOG[job.fault_id], llm, c.max_steps, run_dir=run_dir)
        except EpisodeAborted as exc:
            spent += exc.cost_usd
            failures[c.provider] = failures.get(c.provider, 0) + 1
            log(f"    not counted, will retry next run: {exc}")
            if failures[c.provider] == MAX_CONSECUTIVE_API_FAILURES:
                log(
                    f"    skipping remaining {c.provider} jobs this run "
                    f"({MAX_CONSECUTIVE_API_FAILURES} API failures in a row; out of credits?)"
                )
            continue
        except Exception as exc:
            # A harness or cluster problem: also retried next run
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "error.txt").write_text(f"{type(exc).__name__}: {exc}\n")
            log(f"    error, will retry next run: {type(exc).__name__}: {exc}")
            continue
        failures[c.provider] = 0
        spent += (r.agent_cost_usd or 0) + (r.judge_cost_usd or 0)
        verdict = "resolved" if r.resolved else "NOT resolved"
        log(
            f"    {verdict}, {r.root_cause_verdict}, {r.fix_label}, {r.steps} steps, "
            f"${(r.agent_cost_usd or 0):.3f}  (total ${spent:.2f})"
        )
        results.append({"label": c.label, **vars(r)})
    return results


def _spent(run_dir: Path) -> float:
    """Money spent on a job so far: its result, plus any aborted attempts."""
    total = 0.0
    result = run_dir / "result.json"
    if result.exists():
        r = json.loads(result.read_text())
        total += (r.get("agent_cost_usd") or 0) + (r.get("judge_cost_usd") or 0)
    errors = run_dir / "errors.jsonl"
    if errors.exists():
        total += sum(
            json.loads(line).get("cost_usd", 0) for line in errors.read_text().splitlines() if line
        )
    return total
