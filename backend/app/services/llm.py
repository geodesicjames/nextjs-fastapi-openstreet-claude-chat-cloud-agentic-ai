"""The boundary between the app and the Claude API.

The chat service talks to a `ChatModel`: something that streams a reply as
`TextDelta`s and finishes with one `ModelReply`. `AnthropicChatModel` is the
real one (the official SDK's async streaming helper); tests use a scripted
fake, the same way they swap in a fake email sender. Keeping the SDK behind
this small interface means the agent loop can be tested without network
calls or API costs, and the rest of the app never imports `anthropic`.

Request settings applied on every call (see the Claude API docs):
- `max_tokens`: always set; thinking counts towards it.
- `thinking: adaptive` + `output_config.effort`: the model decides how much
  to reason; effort (CHAT_EFFORT, "low" for chat) is the cost/quality lever.
- top-level `cache_control`: caches the request prefix (system prompt and
  conversation so far), so each new turn re-reads history at ~10% of the
  input price.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import anthropic
from anthropic.types import MessageParam, ToolChoiceParam, ToolParam

from app.services.pricing import TokenUsage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ModelReply:
    """The finished assistant turn."""

    # Content blocks exactly as the API returned them, as JSON-ready dicts:
    # text, thinking (often with empty text but a signature that must be
    # kept), and later tool_use. Stored and replayed unchanged.
    content: list[dict[str, Any]]
    stop_reason: str | None  # end_turn | max_tokens | refusal | tool_use | ...
    usage: TokenUsage
    model: str  # the model requested (prices are looked up by this)
    # The model the API says actually answered. Normally equal to `model`;
    # evals assert it, since a silently substituted model invalidates them.
    served_model: str = ""


StreamItem = TextDelta | ModelReply

# The API's effort levels (the same values CHAT_EFFORT accepts).
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ModelUnavailableError(Exception):
    """The API couldn't be reached or refused the request (not a model refusal)."""


class ChatModel(Protocol):
    def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[MessageParam],
        max_tokens: int,
        effort: Effort,
        tools: list[ToolParam] | None = None,
        tool_choice: ToolChoiceParam | None = None,
    ) -> AsyncIterator[StreamItem]:
        """Yield TextDeltas as they arrive, then exactly one ModelReply."""
        ...


class AnthropicChatModel:
    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self.client = client

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[MessageParam],
        max_tokens: int,
        effort: Effort,
        tools: list[ToolParam] | None = None,
        tool_choice: ToolChoiceParam | None = None,
    ) -> AsyncIterator[StreamItem]:
        try:
            async with self.client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
                tools=tools if tools is not None else anthropic.omit,
                tool_choice=tool_choice if tool_choice is not None else anthropic.omit,
            ) as stream:
                async for text in stream.text_stream:
                    yield TextDelta(text)
                message = await stream.get_final_message()
        # Most specific first: rate limits and API errors are retried by the
        # SDK (2 retries) before they reach us.
        except anthropic.RateLimitError as error:
            logger.warning("Claude API rate limited: %s", error)
            raise ModelUnavailableError("rate_limited") from error
        except anthropic.APIStatusError as error:
            logger.error("Claude API error %s: %s", error.status_code, error)
            raise ModelUnavailableError("api_error") from error
        except anthropic.APIConnectionError as error:
            logger.error("Claude API unreachable: %s", error)
            raise ModelUnavailableError("connection_error") from error

        usage = message.usage
        if message.model != model:
            logger.warning("Requested %s but %s answered", model, message.model)
        yield ModelReply(
            # exclude_none drops unset optional fields (e.g. text blocks'
            # `citations: null`) so the stored blocks replay cleanly.
            content=[block.model_dump(mode="json", exclude_none=True) for block in message.content],
            stop_reason=message.stop_reason,
            usage=TokenUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
                cache_read_input_tokens=usage.cache_read_input_tokens or 0,
            ),
            model=model,
            served_model=message.model,
        )
