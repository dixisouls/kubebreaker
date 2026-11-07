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

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_tokens + other.cached_tokens,
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    # Provider-native assistant output, sent back unchanged to the same provider so it keeps
    # its reasoning state. Adapters fall back to text/tool_calls when it's missing.
    raw: Any = None
    raw_provider: str | None = None


Stop = Literal["tool_use", "end", "max_tokens"]


@dataclass
class LLMResponse:
    message: Message
    usage: Usage
    stop: Stop


class LLMClient(Protocol):
    provider: str
    model: str

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...
