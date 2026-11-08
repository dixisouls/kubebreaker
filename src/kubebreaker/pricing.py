"""Per-model prices in USD per million tokens, used to put a cost on every episode.

Sources (checked 2026-11-08):
- gpt-6.1-terra: OpenAI launch announcement, https://openai.com/index/introducing-gpt-6-1-terra/
  (input $1, cached input $0.05, output $5; requests above ~272K input tokens cost more,
  which episodes never reach)
- claude-sonnet-5-5: Anthropic pricing (input $2, output $10, cache reads $0.20,
  5-minute cache writes $2.50)

Add a model here before benchmarking it, or its cost is reported as unknown.
"""

from dataclasses import dataclass

from kubebreaker.llm import Usage


@dataclass(frozen=True)
class Price:
    input: float
    cached_input: float
    output: float
    cache_write: float | None = None  # None: cache writes bill as regular input


PRICES: dict[str, Price] = {
    "gpt-6.1-terra": Price(input=1.00, cached_input=0.05, output=5.00),
    "claude-sonnet-5-5": Price(input=2.00, cached_input=0.20, output=10.00, cache_write=2.50),
}


def cost_usd(model: str, usage: Usage) -> float | None:
    """Dollar cost of `usage` on `model`, or None when the model has no price entry."""
    price = PRICES.get(model)
    if price is None:
        return None
    uncached = max(0, usage.input_tokens - usage.cached_tokens - usage.cache_write_tokens)
    write_rate = price.cache_write if price.cache_write is not None else price.input
    total = (
        uncached * price.input
        + usage.cached_tokens * price.cached_input
        + usage.cache_write_tokens * write_rate
        + usage.output_tokens * price.output
    )
    return round(total / 1_000_000, 6)
