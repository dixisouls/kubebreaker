"""The agent loop, driven by a scripted fake LLM. No cluster or API calls."""

from kubebreaker.agent.loop import run_agent
from kubebreaker.agent.tools import SPECS, redact_secret, truncate
from kubebreaker.agent.trace import Trace
from kubebreaker.llm import LLMResponse, Message, ToolCall, Usage


class ScriptedLLM:
    provider, model = "fake", "fake-1"

    def __init__(self, turns: list[list[ToolCall] | str]):
        self.turns = list(turns)
        self.seen: list[list[Message]] = []

    def chat(self, system, messages, tools):
        self.seen.append(list(messages))
        turn = self.turns.pop(0) if self.turns else "nothing to do"
        if isinstance(turn, str):
            return LLMResponse(Message("assistant", turn), Usage(100, 10), "end")
        return LLMResponse(Message("assistant", None, turn), Usage(100, 10), "tool_use")


class RecordingTools:
    specs = SPECS

    def __init__(self):
        self.calls = []

    def run(self, name, args):
        self.calls.append((name, args))
        return f"ok:{name}"


def call(i, tool, **args):
    return ToolCall(f"c{i}", tool, {"purpose": f"p{i}", **args})


def test_runs_tools_until_declared():
    llm = ScriptedLLM(
        [
            [call(1, "get_resources", kind="pods")],
            [
                call(
                    2, "apply_patch", kind="deployment", name="api", patch={"spec": {"replicas": 2}}
                )
            ],
            [
                ToolCall(
                    "c3", "declare_resolved", {"root_cause": "api at 0", "fix_summary": "scaled"}
                )
            ],
        ]
    )
    tools = RecordingTools()
    trace = Trace(None)
    out = run_agent(llm, tools, "frontend errors", trace)

    assert out.declared and out.stop_reason == "declared"
    assert out.root_cause == "api at 0" and out.fix_summary == "scaled"
    assert out.steps == 3 and out.turns == 3
    assert out.usage.input_tokens == 300
    assert [c[0] for c in tools.calls] == ["get_resources", "apply_patch"]
    purposes = [e["purpose"] for e in trace.events if e["kind"] == "tool"]
    assert purposes == ["p1", "p2"]
    # The tool result for call 1 was sent back on the next turn
    last = llm.seen[1][-1]
    assert last.role == "tool" and last.tool_results[0].output == "ok:get_resources"


def test_stops_at_step_cap():
    llm = ScriptedLLM([[call(i, "get_events")] for i in range(50)])
    out = run_agent(llm, RecordingTools(), "alert", Trace(None), max_steps=5)
    assert out.stop_reason == "max_steps" and out.steps == 5 and not out.declared


def test_stops_at_token_cap():
    llm = ScriptedLLM([[call(i, "get_events")] for i in range(50)])
    out = run_agent(llm, RecordingTools(), "alert", Trace(None), max_tokens=250)
    assert out.stop_reason == "max_tokens" and out.turns == 3


def test_nudges_then_gives_up_when_model_stops_using_tools():
    llm = ScriptedLLM(["thinking", "still thinking", "done?"])
    out = run_agent(llm, RecordingTools(), "alert", Trace(None))
    assert out.stop_reason == "no_tool_calls" and out.turns == 3
    assert llm.seen[1][-1].role == "user"  # the nudge


def test_llm_error_ends_episode_cleanly():
    class Broken(ScriptedLLM):
        def chat(self, *a):
            raise RuntimeError("rate limited")

    out = run_agent(Broken([]), RecordingTools(), "alert", Trace(None))
    assert out.stop_reason == "llm_error" and "rate limited" in out.error


def test_every_tool_except_final_requires_a_purpose():
    for spec in SPECS:
        required = spec.parameters["required"]
        assert ("purpose" in required) == (spec.name != "declare_resolved"), spec.name


def test_secret_values_are_redacted():
    obj = {
        "metadata": {
            "name": "s",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": '{"data":{"password":"aGk="}}'
            },
        },
        "data": {"password": "aGk="},
    }
    red = redact_secret(obj)
    assert red["data"] == {"password": "<redacted>"}
    assert "aGk=" not in str(red)


def test_truncate_keeps_head_and_tail():
    text = "A" * 5000 + "B" * 5000
    out = truncate(text, 1000)
    assert out.startswith("A" * 500) and out.endswith("B" * 500) and "trimmed" in out


def test_budget_notices_are_separate_system_messages_sent_once():
    from kubebreaker.agent.loop import budget_notice

    sent: set[str] = set()
    assert budget_notice(3, 20, sent) is None
    assert "10 of 20" in budget_notice(10, 20, sent)
    assert budget_notice(12, 20, sent) is None  # halfway notice only once
    assert "3 left" in budget_notice(17, 20, sent)
    assert budget_notice(18, 20, sent) is None  # final notice only once


def test_notices_never_go_inside_tool_results():
    llm = ScriptedLLM([[call(i, "get_events")] for i in range(50)])
    run_agent(llm, RecordingTools(), "alert", Trace(None), max_steps=20)
    history = llm.seen[-1]
    assert all(
        r.output == "ok:get_events" for m in history if m.role == "tool" for r in m.tool_results
    )
    notices = [m.text for m in history if m.role == "system"]
    assert len(notices) == 2 and "left" in notices[1]
    # each notice directly follows a tool-results turn
    for i, m in enumerate(history):
        if m.role == "system":
            assert history[i - 1].role == "tool"


def test_refusal_stops_without_appending_the_turn():
    class Refuses(ScriptedLLM):
        def chat(self, system, messages, tools):
            self.seen.append(list(messages))
            return LLMResponse(Message("assistant"), Usage(5, 0), "refusal", "m-1")

    llm = Refuses([])
    out = run_agent(llm, RecordingTools(), "alert", Trace(None))
    assert out.stop_reason == "refusal" and out.turns == 1


def test_fallback_turns_are_counted():
    class FallsBack(ScriptedLLM):
        def chat(self, system, messages, tools):
            resp = super().chat(system, messages, tools)
            resp.served_by, resp.fallback = "other-model", True
            return resp

    llm = FallsBack([[call(1, "get_events")], [ToolCall("c2", "declare_resolved", {})]])
    out = run_agent(llm, RecordingTools(), "alert", Trace(None))
    assert out.fallback_turns == 2 and out.served_by == ["other-model"]


def test_parallel_calls_cannot_exceed_the_step_cap():
    batch = [call(i, "get_events") for i in range(4)]
    llm = ScriptedLLM([batch, batch, batch])
    tools = RecordingTools()
    trace = Trace(None)
    out = run_agent(llm, tools, "alert", trace, max_steps=6)
    assert out.steps == 6 and len(tools.calls) == 6 and out.stop_reason == "max_steps"
    # the last two calls of the second batch got a result but weren't executed or counted
    assert [e["kind"] for e in trace.events].count("over_budget") == 2
