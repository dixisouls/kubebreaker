"""The agent loop: send the alert, run tool calls, stop on declare_resolved or a cap."""

import time
from dataclasses import dataclass, field
from typing import Protocol

from kubebreaker.agent.trace import Trace
from kubebreaker.llm import LLMClient, Message, ToolResult, ToolSpec, Usage

SYSTEM_PROMPT = """\
You are the on-call SRE for a small online shop running on Kubernetes. An alert has fired.
Find the root cause and fix it, using only the tools you have.

How to work:
- Observe first: look at what's failing before changing anything.
- Every tool call takes a short `purpose`: what the call checks or changes, and why.
- Narrow down the cause with targeted reads (resources, describe, logs, events, metrics).
- Fix the actual cause with the smallest correct change. Avoid band-aids such as restarting
  or deleting pods repeatedly, removing resource limits, or changing unrelated objects.
- After a change, verify the app is healthy again (rollouts complete, endpoints ready,
  errors gone). Changes can take a few seconds to settle.
- When it's fixed and verified, call declare_resolved with a specific root cause (which object,
  what was wrong) and what you changed.

The app runs in namespace `shop`. Cluster DNS (CoreDNS) runs in `kube-system`.
"""

NUDGE = "Continue the investigation with your tools, or call declare_resolved if it's fixed."


class Tools(Protocol):
    @property
    def specs(self) -> list[ToolSpec]: ...

    def run(self, name: str, args: dict) -> str: ...


@dataclass
class AgentOutcome:
    declared: bool = False
    root_cause: str = ""
    fix_summary: str = ""
    steps: int = 0  # tool calls, including declare_resolved
    turns: int = 0  # LLM calls
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = ""
    error: str = ""
    seconds: float = 0.0
    # Turns a provider-side fallback model produced instead of the requested one
    fallback_turns: int = 0
    served_by: list[str] = field(default_factory=list)


def alert_message(alert: str, max_steps: int) -> str:
    return (
        f"ALERT FIRING\n\n{alert}\n\nInvestigate and resolve this incident. "
        f"You have a budget of {max_steps} tool calls, including declare_resolved."
    )


def budget_notice(used: int, max_steps: int, sent: set[str]) -> str | None:
    """A step-budget notice at two milestones, each sent once: halfway, and when 5 or fewer
    calls remain. Sent as a separate system message, never inside a tool result."""
    left = max_steps - used
    if 0 < left <= 5 and "final" not in sent:
        sent.add("final")
        return (
            f"Step budget: {used} of {max_steps} tool calls used, {left} left. "
            "Finish verifying and call declare_resolved."
        )
    if used * 2 >= max_steps and left > 5 and "half" not in sent:
        sent.add("half")
        return f"Step budget: {used} of {max_steps} tool calls used."
    return None


def run_agent(
    llm: LLMClient,
    tools: Tools,
    alert: str,
    trace: Trace,
    max_steps: int = 20,
    max_tokens: int = 600_000,
    final_tool: str = "declare_resolved",
) -> AgentOutcome:
    out = AgentOutcome()
    start = time.monotonic()
    messages = [Message("user", alert_message(alert, max_steps))]
    trace.log("alert", alert=alert, provider=llm.provider, model=llm.model)
    idle_turns = 0
    notices_sent: set[str] = set()

    while True:
        if out.steps >= max_steps:
            out.stop_reason = "max_steps"
            break
        if out.usage.total >= max_tokens:
            out.stop_reason = "max_tokens"
            break

        try:
            resp = llm.chat(SYSTEM_PROMPT, messages, tools.specs)
        except Exception as exc:
            out.stop_reason, out.error = "llm_error", f"{type(exc).__name__}: {exc}"
            trace.log("llm_error", error=out.error)
            break
        out.turns += 1
        out.usage = out.usage + resp.usage
        out.fallback_turns += resp.fallback
        if resp.served_by and resp.served_by not in out.served_by:
            out.served_by.append(resp.served_by)
        trace.log(
            "llm",
            turn=out.turns,
            text=resp.message.text,
            stop=resp.stop,
            usage=resp.usage,
            served_by=resp.served_by,
            fallback=resp.fallback,
            tool_calls=[{"id": c.id, "name": c.name} for c in resp.message.tool_calls],
        )
        if resp.stop == "refusal":
            out.stop_reason = "refusal"
            break
        messages.append(resp.message)

        if not resp.message.tool_calls:
            idle_turns += 1
            if idle_turns >= 3:
                out.stop_reason = "no_tool_calls"
                break
            messages.append(Message("user", NUDGE))
            continue
        idle_turns = 0

        results = []
        for call in resp.message.tool_calls:
            out.steps += 1
            if call.name == final_tool:
                out.declared = True
                out.root_cause = str(call.args.get("root_cause", ""))
                out.fix_summary = str(call.args.get("fix_summary", ""))
                trace.log("declare_resolved", step=out.steps, **call.args)
                results.append(ToolResult(call.id, "Incident closed."))
                continue
            t0 = time.monotonic()
            output = tools.run(call.name, call.args)
            trace.log(
                "tool",
                step=out.steps,
                name=call.name,
                purpose=call.args.get("purpose", ""),
                args={k: v for k, v in call.args.items() if k != "purpose"},
                output=output,
                seconds=round(time.monotonic() - t0, 3),
            )
            results.append(ToolResult(call.id, output))
        messages.append(Message("tool", tool_results=results))
        notice = None if out.declared else budget_notice(out.steps, max_steps, notices_sent)
        if notice:
            messages.append(Message("system", notice))
            trace.log("notice", text=notice)

        if out.declared:
            out.stop_reason = "declared"
            break

    out.seconds = time.monotonic() - start
    trace.log("agent_done", **{k: v for k, v in vars(out).items() if k != "usage"}, usage=out.usage)
    return out
