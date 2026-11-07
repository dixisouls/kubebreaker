"""Conversion between neutral types and each provider's wire format. No API calls."""

from types import SimpleNamespace as NS

from kubebreaker.llm import Message, ToolCall, ToolResult, ToolSpec
from kubebreaker.llm import anthropic_client as anth
from kubebreaker.llm import openai_client as oai

SPEC = ToolSpec("get_logs", "Tail logs", {"type": "object", "properties": {}})
HISTORY = [
    Message("user", "alert!"),
    Message("assistant", "checking", [ToolCall("c1", "get_logs", {"pod": "api-1"})]),
    Message("tool", tool_results=[ToolResult("c1", "line 1")]),
]


class FakeItem(NS):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


# --- OpenAI ---


def test_openai_input_from_neutral_history():
    items = oai.to_input(HISTORY)
    assert items[0] == {"role": "user", "content": "alert!"}
    assert items[1] == {"role": "assistant", "content": "checking"}
    assert items[2] == {
        "type": "function_call",
        "call_id": "c1",
        "name": "get_logs",
        "arguments": '{"pod": "api-1"}',
    }
    assert items[3] == {"type": "function_call_output", "call_id": "c1", "output": "line 1"}


def test_openai_echoes_raw_output_back_unchanged():
    raw = [{"type": "reasoning", "encrypted_content": "xyz"}, {"type": "function_call"}]
    msg = Message("assistant", raw=raw, raw_provider="openai")
    assert oai.to_input([msg]) == raw


def test_openai_ignores_raw_from_other_provider():
    msg = Message("assistant", "hi", raw=[{"type": "text"}], raw_provider="anthropic")
    assert oai.to_input([msg]) == [{"role": "assistant", "content": "hi"}]


def test_openai_tools_format():
    assert oai.to_tools([SPEC]) == [
        {
            "type": "function",
            "name": "get_logs",
            "description": "Tail logs",
            "parameters": {"type": "object", "properties": {}},
        }
    ]


def test_openai_response_parsing():
    resp = NS(
        output=[
            FakeItem(type="reasoning", id="r1"),
            FakeItem(
                type="function_call", call_id="c9", name="describe", arguments='{"kind":"pod"}'
            ),
        ],
        output_text="",
        status="completed",
        usage=NS(input_tokens=100, output_tokens=20, input_tokens_details=NS(cached_tokens=40)),
    )
    r = oai.from_response(resp)
    assert r.stop == "tool_use"
    assert r.message.tool_calls == [ToolCall("c9", "describe", {"kind": "pod"})]
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cached_tokens) == (100, 20, 40)
    assert r.message.raw_provider == "openai" and len(r.message.raw) == 2


def test_openai_bad_json_arguments_are_kept_not_crashed():
    assert oai.parse_args("{not json") == {"_invalid_arguments": "{not json"}


# --- Anthropic ---


def test_anthropic_messages_from_neutral_history():
    msgs = anth.to_messages(HISTORY)
    assert msgs[0] == {"role": "user", "content": "alert!"}
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0] == {"type": "text", "text": "checking"}
    assert msgs[1]["content"][1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "get_logs",
        "input": {"pod": "api-1"},
    }
    # Tool results go back as a user turn, and the last block carries the cache marker
    assert msgs[2]["role"] == "user"
    block = msgs[2]["content"][0]
    assert block["type"] == "tool_result" and block["tool_use_id"] == "c1"
    assert block["cache_control"] == {"type": "ephemeral"}


def test_anthropic_cache_marker_does_not_leak_into_stored_raw():
    raw = [{"type": "text", "text": "hi"}]
    msg = Message("assistant", raw=raw, raw_provider="anthropic")
    for _ in range(3):
        anth.to_messages([msg])
    assert raw == [{"type": "text", "text": "hi"}]


def test_anthropic_tools_format_caches_last_tool():
    tools = anth.to_tools([SPEC, SPEC])
    assert tools[0] == {
        "name": "get_logs",
        "description": "Tail logs",
        "input_schema": {"type": "object", "properties": {}},
    }
    assert tools[1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_response_parsing_counts_cache_tokens_as_input():
    resp = NS(
        content=[
            FakeItem(type="text", text="looking"),
            FakeItem(type="tool_use", id="t1", name="get_events", input={"namespace": "shop"}),
        ],
        stop_reason="tool_use",
        usage=NS(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=300,
            cache_creation_input_tokens=50,
        ),
    )
    r = anth.from_response(resp)
    assert r.stop == "tool_use"
    assert r.message.text == "looking"
    assert r.message.tool_calls == [ToolCall("t1", "get_events", {"namespace": "shop"})]
    assert (r.usage.input_tokens, r.usage.cached_tokens) == (360, 300)
