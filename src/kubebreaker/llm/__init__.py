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


def make_client(
    provider: str | None = None, model: str | None = None, effort: str | None = None
) -> LLMClient:
    """Build a client. The model comes from the argument, then LLM_MODEL when the provider is
    LLM_PROVIDER, then <PROVIDER>_MODEL (OPENAI_MODEL, ANTHROPIC_MODEL)."""
    default_provider = os.environ.get("LLM_PROVIDER", "").lower()
    provider = (provider or default_provider).lower()
    if not model and provider == default_provider:
        model = os.environ.get("LLM_MODEL")
    model = model or os.environ.get(f"{provider.upper()}_MODEL", "")
    if not model:
        raise ValueError(
            f"no model set for {provider!r}: set LLM_MODEL or {provider.upper()}_MODEL"
        )
    max_out = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "16000"))
    effort = effort or os.environ.get("LLM_REASONING_EFFORT") or None

    # Imported lazily so you only need the SDK for the provider you use
    if provider == "openai":
        from kubebreaker.llm.openai_client import OpenAIClient

        return OpenAIClient(model, max_out, effort)
    if provider == "anthropic":
        from kubebreaker.llm.anthropic_client import AnthropicClient

        fallbacks = os.environ.get("LLM_FALLBACKS", "default").lower() != "off"
        # Explicit effort; medium is the recommended start for multistep tool use
        return AnthropicClient(model, max_out, effort or "medium", fallbacks)
    raise ValueError(f"unknown provider {provider!r}: use 'openai' or 'anthropic'")
