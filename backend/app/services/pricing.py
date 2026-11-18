"""What each Claude call costs, from the model's published per-token prices.

Prices are USD per million tokens, from Anthropic's pricing page (checked
2026-11-18). Update this table when prices change or a model is added; the
settings refuse to start with a CHAT_MODEL that isn't listed here, so a
model switch can never silently log $0.

Prompt caching changes the price of input tokens:
- cache writes (5-minute TTL) cost 1.25x the input price;
- cache reads cost a fraction of it (per model below).
Claude Haiku 5.5 also has a long-context rate card: prompts over 100K tokens
are billed at higher prices for the whole request.
"""

from dataclasses import dataclass
from decimal import Decimal

MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Rates:
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal  # 5-minute TTL (the default)


@dataclass(frozen=True)
class ModelPrice:
    standard: Rates
    # (prompt-token threshold, rates above it), for models with a long-context
    # rate card.
    long_context: tuple[int, Rates] | None = None


def _rates(input: str, output: str, cache_read: str) -> Rates:
    i = Decimal(input)
    return Rates(
        input=i,
        output=Decimal(output),
        cache_read=Decimal(cache_read),
        cache_write=i * Decimal("1.25"),
    )


PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(
        standard=_rates("0.10", "0.50", "0.01"),
        long_context=(100_000, _rates("0.50", "2.50", "0.05")),
    ),
    "claude-sonnet-5-5": ModelPrice(standard=_rates("2.00", "10.00", "0.10")),
    "claude-opus-5-5": ModelPrice(standard=_rates("4.00", "20.00", "0.20")),
}


@dataclass(frozen=True)
class TokenUsage:
    """The `usage` fields of one Messages API response."""

    input_tokens: int  # uncached input
    output_tokens: int  # includes thinking tokens
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


def cost_usd(model: str, usage: TokenUsage) -> Decimal:
    """The cost of one call. Raises KeyError for a model without prices."""
    price = PRICES[model]
    rates = price.standard
    if price.long_context and usage.prompt_tokens > price.long_context[0]:
        rates = price.long_context[1]
    total = (
        usage.input_tokens * rates.input
        + usage.output_tokens * rates.output
        + usage.cache_creation_input_tokens * rates.cache_write
        + usage.cache_read_input_tokens * rates.cache_read
    ) / MILLION
    return total.quantize(Decimal("0.000001"))
