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
- Every tool call takes a `hypothesis`: write your current best guess about the root cause
  and what the call will confirm or rule out.
- Test hypotheses with targeted reads (resources, describe, logs, events, metrics).
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


def alert_message(alert: str, max_steps: int) -> str:
    return (
        f"ALERT FIRING\n\n{alert}\n\nInvestigate and resolve this incident. "
        f"You have a budget of {max_steps} tool calls, including declare_resolved."
    )


def budget_note(used: int, max_steps: int) -> str:
    left = max_steps - used
    note = f"[budget: {used} of {max_steps} tool calls used]"
    if 0 < left <= 5:
        note += f" Only {left} left: finish verifying and call declare_resolved."
    return note


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
        messages.append(resp.message)
        trace.log(
            "llm",
            turn=out.turns,
            text=resp.message.text,
            stop=resp.stop,
            usage=resp.usage,
            tool_calls=[{"id": c.id, "name": c.name} for c in resp.message.tool_calls],
        )

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
                hypothesis=call.args.get("hypothesis", ""),
                args={k: v for k, v in call.args.items() if k != "hypothesis"},
                output=output,
                seconds=round(time.monotonic() - t0, 3),
            )
            results.append(ToolResult(call.id, output))
        if results and not out.declared:
            last = results[-1]
            results[-1] = ToolResult(
                last.call_id, f"{last.output}\n\n{budget_note(out.steps, max_steps)}"
            )
        messages.append(Message("tool", tool_results=results))

        if out.declared:
            out.stop_reason = "declared"
            break

    out.seconds = time.monotonic() - start
    trace.log("agent_done", **{k: v for k, v in vars(out).items() if k != "usage"}, usage=out.usage)
    return out
