"""Cost estimates per call, and the guard that every chat model has prices."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.services.pricing import PRICES, TokenUsage, cost_usd


def test_haiku_standard_rates() -> None:
    # Prompt within 100K tokens: $0.10 / $0.50 per million.
    usage = TokenUsage(input_tokens=100_000, output_tokens=100_000)
    assert cost_usd("claude-haiku-5-5", usage) == Decimal("0.06")  # $0.01 + $0.05


def test_cache_reads_and_writes_are_priced_separately() -> None:
    usage = TokenUsage(
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=50_000,  # at 1.25 x $0.10
        cache_read_input_tokens=50_000,  # at 0.1 x $0.10
    )
    assert cost_usd("claude-haiku-5-5", usage) == Decimal("0.00675")  # $0.00625 + $0.0005


def test_haiku_long_context_rates_apply_above_100k_prompt_tokens() -> None:
    at_threshold = TokenUsage(input_tokens=60_000, cache_read_input_tokens=40_000, output_tokens=0)
    over = TokenUsage(input_tokens=60_002, cache_read_input_tokens=40_000, output_tokens=0)

    # At 100K: 60K x $0.10 + 40K x $0.01 = $0.0064. Just over, the whole
    # request moves to the long-context card: 60,002 x $0.50 + 40K x $0.05.
    assert cost_usd("claude-haiku-5-5", at_threshold) == Decimal("0.0064")
    assert cost_usd("claude-haiku-5-5", over) == Decimal("0.032001")


def test_opus_rates() -> None:
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=100_000)
    assert cost_usd("claude-opus-5-5", usage) == Decimal("6.00")  # $4 + $2


def test_default_chat_model_has_prices() -> None:
    assert get_settings().chat_model in PRICES


def test_unknown_chat_model_is_rejected_at_startup() -> None:
    with pytest.raises(ValidationError, match="no prices for 'claude-made-up'"):
        Settings(chat_model="claude-made-up")  # pyright: ignore[reportCallIssue]
