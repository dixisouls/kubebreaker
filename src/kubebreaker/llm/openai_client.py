"""OpenAI adapter, built on the Responses API (newer models only support function tools there)."""

import json

from openai import OpenAI

from kubebreaker.llm.types import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
)

PROVIDER = "openai"


def to_input(messages: list[Message]) -> list[dict]:
    items: list[dict] = []
    for m in messages:
        if m.role == "user":
            items.append({"role": "user", "content": m.text or ""})
        elif m.role == "system":
            # Harness notices go in as developer messages, separate from tool output
            items.append({"role": "developer", "content": m.text or ""})
        elif m.role == "assistant":
            if m.raw is not None and m.raw_provider == PROVIDER:
                items.extend(m.raw)
                continue
            if m.text:
                items.append({"role": "assistant", "content": m.text})
            for c in m.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": c.id,
                        "name": c.name,
                        "arguments": json.dumps(c.args),
                    }
                )
        elif m.role == "tool":
            for r in m.tool_results:
                items.append(
                    {"type": "function_call_output", "call_id": r.call_id, "output": r.output}
                )
    return items


def to_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        }
        for t in tools
    ]


def parse_args(raw: str) -> dict:
    try:
        args = json.loads(raw or "{}")
        return args if isinstance(args, dict) else {"_invalid_arguments": raw}
    except json.JSONDecodeError:
        return {"_invalid_arguments": raw}


def from_response(resp) -> LLMResponse:
    calls, raw = [], []
    for item in resp.output:
        raw.append(item.model_dump(exclude_none=True))
        if item.type == "function_call":
            calls.append(ToolCall(item.call_id, item.name, parse_args(item.arguments)))
    u = resp.usage
    details = getattr(u, "input_tokens_details", None)
    usage = Usage(
        u.input_tokens,
        u.output_tokens,
        getattr(details, "cached_tokens", 0) or 0,
        getattr(details, "cache_write_tokens", 0) or 0,
    )
    if calls:
        stop = "tool_use"
    elif resp.status == "incomplete":
        stop = "max_tokens"
    else:
        stop = "end"
    msg = Message("assistant", resp.output_text or None, calls, raw=raw, raw_provider=PROVIDER)
    return LLMResponse(msg, usage, stop)


class OpenAIClient:
    provider = PROVIDER

    def __init__(
        self, model: str, max_output_tokens: int = 4096, reasoning_effort: str | None = None
    ):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self._client = OpenAI(max_retries=3)

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        kwargs = {}
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        if tools:
            kwargs["tools"] = to_tools(tools)
        resp = self._client.responses.create(
            model=self.model,
            instructions=system,
            input=to_input(messages),
            max_output_tokens=self.max_output_tokens,
            # Stateless: we send the full history each turn, including encrypted reasoning
            store=False,
            include=["reasoning.encrypted_content"],
            **kwargs,
        )
        return from_response(resp)
