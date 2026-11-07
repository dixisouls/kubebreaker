"""LLM access. Pick a provider and model with LLM_PROVIDER and LLM_MODEL."""

import os

from kubebreaker.llm.types import (
    LLMClient,
    LLMResponse,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)

__all__ = [
    "LLMClient",
    "LLMResponse",
    "Message",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "make_client",
]


def make_client(provider: str | None = None, model: str | None = None) -> LLMClient:
    provider = (provider or os.environ.get("LLM_PROVIDER", "")).lower()
    model = model or os.environ.get("LLM_MODEL", "")
    if not model:
        raise ValueError("no model set: pass one or set LLM_MODEL")
    max_out = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "4096"))

    # Imported lazily so you only need the SDK for the provider you use
    if provider == "openai":
        from kubebreaker.llm.openai_client import OpenAIClient

        return OpenAIClient(model, max_out, os.environ.get("LLM_REASONING_EFFORT") or None)
    if provider == "anthropic":
        from kubebreaker.llm.anthropic_client import AnthropicClient

        return AnthropicClient(model, max_out)
    raise ValueError(f"unknown LLM_PROVIDER {provider!r}: use 'openai' or 'anthropic'")
