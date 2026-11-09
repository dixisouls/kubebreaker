"""Google Gemini adapter, used only as the root-cause judge.

It handles text-only calls (system prompt plus a conversation). Tool calling isn't wired up:
using Gemini as the SRE agent would need its function-calling and thought-signature replay,
which nothing here exercises, so a call with tools raises instead of half-working.
"""

from google import genai
from google.genai import types

from kubebreaker.llm.types import LLMResponse, Message, ToolSpec, Usage

PROVIDER = "google"
LEVELS = {"minimal": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}


def to_contents(messages: list[Message]) -> list[types.Content]:
    contents = []
    for m in messages:
        if m.role in ("user", "system"):
            # Gemini has no mid-conversation system role; notices go in as user text
            contents.append(types.Content(role="user", parts=[types.Part(text=m.text or "")]))
        elif m.role == "assistant":
            contents.append(types.Content(role="model", parts=[types.Part(text=m.text or "")]))
        else:
            raise NotImplementedError("tool results aren't supported by the Gemini adapter")
    return contents


def from_response(resp) -> LLMResponse:
    u = resp.usage_metadata
    usage = Usage(
        input_tokens=u.prompt_token_count or 0,
        # Thinking tokens are billed as output
        output_tokens=(u.candidates_token_count or 0) + (u.thoughts_token_count or 0),
        cached_tokens=u.cached_content_token_count or 0,
    )
    text = resp.text or ""
    finish = resp.candidates[0].finish_reason if resp.candidates else None
    stop = "max_tokens" if finish == types.FinishReason.MAX_TOKENS else "end"
    if finish == types.FinishReason.SAFETY:
        stop = "refusal"
    msg = Message("assistant", text or None)
    return LLMResponse(msg, usage, stop, resp.model_version)


class GoogleClient:
    provider = PROVIDER

    def __init__(self, model: str, max_output_tokens: int = 16000, effort: str | None = None):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.effort = effort
        self._client = genai.Client()  # reads GOOGLE_API_KEY

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        if tools:
            raise NotImplementedError(
                "Gemini is wired up as a judge only; tool calls aren't supported"
            )
        thinking = None
        if self.effort:
            level = LEVELS.get(self.effort.lower())
            if level is None:
                raise ValueError(
                    f"unknown Gemini thinking level {self.effort!r}: use {list(LEVELS)}"
                )
            thinking = types.ThinkingConfig(thinking_level=level)
        resp = self._client.models.generate_content(
            model=self.model,
            contents=to_contents(messages),
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=self.max_output_tokens,
                thinking_config=thinking,
            ),
        )
        return from_response(resp)
