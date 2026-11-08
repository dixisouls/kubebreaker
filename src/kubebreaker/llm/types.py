"""Provider-neutral types. The harness only ever sees these, never a provider SDK."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON schema for the arguments


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    output: str


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0  # part of input_tokens served from the provider's prompt cache
    cache_write_tokens: int = 0  # part of input_tokens written to the prompt cache

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_tokens + other.cached_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Message:
    # "system" is a harness notice sent mid-conversation (e.g. a step-budget warning), kept
    # separate from tool results so the model doesn't mistake it for tool output
    role: Literal["user", "assistant", "tool", "system"]
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    # Provider-native assistant output, sent back unchanged to the same provider so it keeps
    # its reasoning state. Adapters fall back to text/tool_calls when it's missing.
    raw: Any = None
    raw_provider: str | None = None


Stop = Literal["tool_use", "end", "max_tokens", "refusal"]


@dataclass
class LLMResponse:
    message: Message
    usage: Usage
    stop: Stop
    # The model that actually produced this turn; differs from the requested model when a
    # provider-side fallback took over
    served_by: str | None = None
    fallback: bool = False


class LLMClient(Protocol):
    provider: str
    model: str

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...
