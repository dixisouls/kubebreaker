"""Anthropic adapter, built on the Messages API.

- Thinking is left at the model default (adaptive); depth is set with `output_config.effort`.
- Assistant turns are replayed exactly as returned (thinking blocks included) and the history
  is append-only, so preserved-thinking checks pass and the prompt cache stays warm.
- Harness notices go in mid-conversation `system` messages, never inside tool results.
- Server-side refusal fallback is on by default; the turn records which model served it so a
  benchmark can tell when a different model did the work.
"""

from anthropic import Anthropic

from kubebreaker.llm.types import LLMResponse, Message, ToolCall, ToolSpec, Usage

PROVIDER = "anthropic"
CACHE = {"type": "ephemeral"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def to_messages(messages: list[Message]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text or ""})
        elif m.role == "system":
            out.append({"role": "system", "content": m.text or ""})
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
    # Cache the conversation so far: each turn re-sends the whole history. Moving this marker
    # between requests doesn't count as a history edit.
    for msg in reversed(out):
        if isinstance(msg["content"], list) and msg["content"]:
            msg["content"][-1] = {**msg["content"][-1], "cache_control": CACHE}
            break
    return out


def to_tools(tools: list[ToolSpec]) -> list[dict]:
    specs = [
        {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
    ]
    if specs:
        specs[-1] = {**specs[-1], "cache_control": CACHE}
    return specs


def _usage(u) -> Usage:
    cached = getattr(u, "cache_read_input_tokens", 0) or 0
    created = getattr(u, "cache_creation_input_tokens", 0) or 0
    # Anthropic reports cache reads and writes separately from input_tokens
    return Usage((u.input_tokens or 0) + cached + created, u.output_tokens or 0, cached)


def from_response(resp) -> LLMResponse:
    iterations = getattr(resp.usage, "iterations", None) or []
    fallback = any(getattr(i, "type", "") == "fallback_message" for i in iterations)
    # Top-level usage covers only the attempt that produced the message; with a fallback,
    # every attempt is in usage.iterations
    if fallback:
        usage = Usage()
        for i in iterations:
            usage = usage + _usage(i)
    else:
        usage = _usage(resp.usage)

    if resp.stop_reason == "refusal":
        # Content is empty or partial: discard it rather than acting on half a turn
        msg = Message("assistant", None, raw=[], raw_provider=PROVIDER)
        return LLMResponse(msg, usage, "refusal", resp.model, fallback)

    calls, texts, raw = [], [], []
    for block in resp.content:
        if block.type == "fallback":
            continue  # audit marker only; not needed in the replayed history
        raw.append(block.model_dump(exclude_none=True))
        if block.type == "tool_use":
            calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        elif block.type == "text":
            texts.append(block.text)
    stop = "tool_use" if calls else ("max_tokens" if resp.stop_reason == "max_tokens" else "end")
    msg = Message("assistant", "\n".join(texts) or None, calls, raw=raw, raw_provider=PROVIDER)
    return LLMResponse(msg, usage, stop, resp.model, fallback)


class AnthropicClient:
    provider = PROVIDER

    def __init__(
        self,
        model: str,
        max_output_tokens: int = 16000,
        effort: str | None = "medium",
        fallbacks: bool = True,
    ):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.effort = effort
        self.fallbacks = fallbacks
        self._client = Anthropic(max_retries=3)

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        kwargs: dict = {}
        if tools:
            kwargs["tools"] = to_tools(tools)
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if self.fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        resp = self._client.beta.messages.create(
            model=self.model,
            system=[{"type": "text", "text": system, "cache_control": CACHE}],
            messages=to_messages(messages),
            max_tokens=self.max_output_tokens,
            **kwargs,
        )
        return from_response(resp)
