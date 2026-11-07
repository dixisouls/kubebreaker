"""Anthropic adapter, built on the Messages API."""

from anthropic import Anthropic

from kubebreaker.llm.types import LLMResponse, Message, ToolCall, ToolSpec, Usage

PROVIDER = "anthropic"
CACHE = {"type": "ephemeral"}


def to_messages(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text or ""})
        elif m.role == "assistant":
            if m.raw is not None and m.raw_provider == PROVIDER:
                # Copy: the cache marker added below must not leak into the stored history
                out.append({"role": "assistant", "content": list(m.raw)})
                continue
            blocks: list[dict] = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            for c in m.tool_calls:
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
            out.append({"role": "assistant", "content": blocks})
        elif m.role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": r.call_id, "content": r.output}
                        for r in m.tool_results
                    ],
                }
            )
    # Cache the conversation so far: each turn re-sends the whole history
    if out and isinstance(out[-1]["content"], list) and out[-1]["content"]:
        out[-1]["content"][-1] = {**out[-1]["content"][-1], "cache_control": CACHE}
    return out


def to_tools(tools: list[ToolSpec]) -> list[dict]:
    specs = [
        {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
    ]
    if specs:
        specs[-1] = {**specs[-1], "cache_control": CACHE}
    return specs


def from_response(resp) -> LLMResponse:
    calls, texts, raw = [], [], []
    for block in resp.content:
        raw.append(block.model_dump(exclude_none=True))
        if block.type == "tool_use":
            calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        elif block.type == "text":
            texts.append(block.text)
    u = resp.usage
    cached = getattr(u, "cache_read_input_tokens", 0) or 0
    created = getattr(u, "cache_creation_input_tokens", 0) or 0
    # Anthropic reports cached and cache-write tokens separately from input_tokens
    usage = Usage(u.input_tokens + cached + created, u.output_tokens, cached)
    stop = {"tool_use": "tool_use", "max_tokens": "max_tokens"}.get(resp.stop_reason, "end")
    if calls:
        stop = "tool_use"
    msg = Message("assistant", "\n".join(texts) or None, calls, raw=raw, raw_provider=PROVIDER)
    return LLMResponse(msg, usage, stop)


class AnthropicClient:
    provider = PROVIDER

    def __init__(self, model: str, max_output_tokens: int = 4096):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self._client = Anthropic(max_retries=3)

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        resp = self._client.messages.create(
            model=self.model,
            system=[{"type": "text", "text": system, "cache_control": CACHE}],
            messages=to_messages(messages),
            tools=to_tools(tools),
            max_tokens=self.max_output_tokens,
        )
        return from_response(resp)
