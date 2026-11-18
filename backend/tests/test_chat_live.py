"""Opt-in check against the real Claude API (costs a fraction of a cent).

Skipped unless RUN_LIVE_CLAUDE=1 and ANTHROPIC_API_KEY are set, so CI and
normal test runs never call the API:

    RUN_LIVE_CLAUDE=1 uv run pytest tests/test_chat_live.py -s

It covers what the fake model can't: the SDK call itself, the shape of the
stored content blocks, and that a second turn accepts the first turn's
blocks replayed unchanged (thinking signatures included).
"""

import os
from typing import Any, cast

import anthropic
import pytest
from anthropic.types import MessageParam

from app.config import get_settings
from app.services.chat import SYSTEM_PROMPT
from app.services.llm import AnthropicChatModel, ModelReply, TextDelta
from app.services.pricing import cost_usd

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_CLAUDE") != "1" or get_settings().anthropic_api_key is None,
    reason="live Claude API test: set RUN_LIVE_CLAUDE=1 and ANTHROPIC_API_KEY to run",
)


async def turn(model: AnthropicChatModel, messages: list[MessageParam]) -> tuple[str, ModelReply]:
    settings = get_settings()
    text, final = "", None
    async for item in model.stream(
        model=settings.chat_model,
        system=SYSTEM_PROMPT,
        messages=messages,
        max_tokens=1_000,
        effort=settings.chat_effort,
    ):
        if isinstance(item, TextDelta):
            text += item.text
        else:
            final = item
    assert final is not None
    return text, final


async def test_two_turns_with_replayed_history() -> None:
    settings = get_settings()
    assert settings.anthropic_api_key is not None
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    model = AnthropicChatModel(client)
    try:
        first_user: dict[str, Any] = {
            "role": "user",
            "content": [{"type": "text", "text": "Say hello in five words or fewer."}],
        }
        text1, reply1 = await turn(model, [cast(MessageParam, first_user)])
        assert text1.strip()
        assert reply1.stop_reason == "end_turn"
        assert any(b["type"] == "text" for b in reply1.content)
        assert reply1.usage.input_tokens > 0 and reply1.usage.output_tokens > 0

        history = [
            cast(MessageParam, first_user),
            cast(MessageParam, {"role": "assistant", "content": reply1.content}),
            cast(
                MessageParam,
                {"role": "user", "content": [{"type": "text", "text": "Now say goodbye."}]},
            ),
        ]
        text2, reply2 = await turn(model, history)
        assert text2.strip()
        assert reply2.stop_reason == "end_turn"

        total = cost_usd(reply1.model, reply1.usage) + cost_usd(reply2.model, reply2.usage)
        print(
            f"\nturn 1: {text1!r} blocks={[b['type'] for b in reply1.content]} usage={reply1.usage}"
            f"\nturn 2: {text2!r} usage={reply2.usage}\ntotal cost: ${total}"
        )
    finally:
        await client.close()
