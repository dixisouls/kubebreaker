"""Postmortem memory. No cluster or API calls."""

from kubebreaker import sweep
from kubebreaker.agent import memory
from kubebreaker.agent.loop import SYSTEM_PROMPT, run_agent
from kubebreaker.agent.trace import Trace
from kubebreaker.llm import LLMResponse, Message, Usage


def note(i: int) -> memory.Postmortem:
    return memory.Postmortem(f"HighErrorRate: alert {i}", f"cause {i}", f"fix {i}", 5)


def test_store_round_trips_and_appends(tmp_path):
    store = memory.MemoryStore(tmp_path / "m" / "memory.jsonl")
    assert store.load() == []
    store.add(note(1))
    store.add(note(2))
    assert store.load() == [note(1), note(2)]


def test_newest_notes_come_first_within_the_limits():
    notes = [note(i) for i in range(20)]
    chosen = memory.select(notes, limit=3)
    assert [n.root_cause for n in chosen] == ["cause 19", "cause 18", "cause 17"]
    block = memory.prompt_block(chosen)
    assert block.index("cause 19") < block.index("cause 17")
    assert "may still be wrong" in block
    # A tight character budget keeps only what fits
    small = memory.select(notes, max_chars=len(memory.HEADER) + 80)
    assert len(small) == 1
    assert memory.prompt_block([]) == ""


def test_multi_line_alerts_fit_on_one_line():
    n = memory.Postmortem("HighErrorRate: a\nHighLatency: b", "c", "f", 3)
    assert "Alert: HighErrorRate: a / HighLatency: b" in memory.prompt_block([n])


def test_memory_is_appended_to_the_system_prompt(tmp_path):
    class LLM:
        provider, model = "fake", "fake-1"
        systems = []

        def chat(self, system, messages, tools):
            self.systems.append(system)
            return LLMResponse(Message("assistant", "done"), Usage(10, 1), "end")

    class Tools:
        specs = []
        mutations = []

    llm = LLM()
    block = memory.prompt_block([note(1)])
    run_agent(llm, Tools(), "ALERT", Trace(tmp_path / "t.jsonl"), max_steps=3, memory=block)
    assert llm.systems[0] == SYSTEM_PROMPT + block


def test_memory_configs_get_one_store_per_label(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "RUNS_DIR", tmp_path / "runs")
    exp = sweep.load("experiments/memory.yaml")
    paths = {j.memory_path(exp.name) for j in exp.jobs()}
    assert {p.parent.name for p in paths} == {"terra-memory", "sonnet-memory"}
    plain = sweep.Job(sweep.Config("x", "openai", "gpt-6.1-terra"), "bad-command", 1)
    assert plain.memory_path("t") is None
