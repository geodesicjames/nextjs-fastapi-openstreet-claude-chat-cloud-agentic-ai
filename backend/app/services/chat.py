"""Conversations with Claude: history, and one streamed, tool-using turn per message.

A turn (`stream_reply`):
1. Lock the conversation row (one turn at a time per conversation).
2. Call the model with the stored history, the new user message and the
   tools (app/services/chat_tools.py), streaming text to the caller.
3. If the model asks for tools, validate and run them, send all results
   back in a single message, and call the model again. Repeat until it
   answers. Guardrails: at most CHAT_MAX_TOOL_STEPS tool steps (then one
   last call with tools disabled, so it answers with what it has), and the
   daily budget is checked again before every further call.
4. Save the whole turn (user message, assistant replies, tool results) and
   every call's usage in one commit.

A turn is only saved if it ends in a state the API accepts when replayed:
an answer, not an unanswered or cut-off tool call. Otherwise (API failure,
refusal mid-tool-call, budget running out) the messages are dropped but the
usage of every call already made is still logged. History is append-only:
rows are never edited, so the replayed prefix stays byte-for-byte identical
(required for replayed thinking blocks, and what makes prompt caching hit).
"""

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from anthropic.types import MessageParam
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ChatMessage, Conversation, User
from app.services.chat_access import ChatStatus, get_chat_status, record_usage
from app.services.chat_tools import TOOLS, ToolContext, ToolOutcome, run_tool
from app.services.llm import ChatModel, ModelReply, ModelUnavailableError, TextDelta

logger = logging.getLogger(__name__)

# Kept byte-for-byte stable (no dates, no per-user text) so it caches.
SYSTEM_PROMPT = """\
You are the assistant inside "Places", a web app with an OpenStreetMap-based \
map for finding cafés, restaurants, parks, toilets, drinking water and \
viewpoints. People ask you where to go; you answer from the app's own map \
data using your tools.

How to work:
- When the user mentions a place by name, call find_location first, then \
search_places around the best match. If the name is ambiguous, pick the \
likeliest match and say which one you used.
- Only recommend places that search_places returned. Never invent \
businesses, addresses, opening hours or prices. If a search finds nothing, \
say so and suggest a larger radius or another category.
- For questions about a specific place (opening hours, website, \
accessibility), find it with search_places using its name, then call \
get_place_details and answer from its tags. If a detail isn't there, say \
it isn't listed.
- Only call save_place when the user asks to save or remember a place.
- If a tool says data may be missing or a service is unavailable, tell the \
user briefly.
- The app shows the places you find on the map next to this chat, so you \
don't need to give coordinates or links.

Keep answers short and practical: a few sentences or a short list with \
names and walking distances (for example "Bean There, 250 m"). Use plain \
language and simple Markdown (no tables). Data comes from OpenStreetMap \
and may be out of date."""

TITLE_LENGTH = 80


# --- Events streamed to the client (serialised as server-sent events) -----------


@dataclass(frozen=True)
class TextEvent:
    text: str
    type: str = "text"


@dataclass(frozen=True)
class DoneEvent:
    message_id: uuid.UUID | None
    stop_reason: str | None
    # True when the reply hit max_tokens and was cut off.
    truncated: bool
    cost_usd: float
    type: str = "done"


@dataclass(frozen=True)
class ErrorEvent:
    # refused | unavailable | busy | budget | truncated
    code: str
    message: str
    type: str = "error"


@dataclass(frozen=True)
class ToolStartEvent:
    tool: str
    input: dict[str, Any]
    type: str = "tool_start"


@dataclass(frozen=True)
class ToolResultEvent:
    tool: str
    ok: bool
    summary: str
    # Places to show on the map: place_id, name, category, lat, lon.
    places: list[dict[str, Any]]
    type: str = "tool_result"


ChatEvent = TextEvent | ToolStartEvent | ToolResultEvent | DoneEvent | ErrorEvent


def event_payload(event: ChatEvent) -> dict[str, Any]:
    data = asdict(event)
    data.pop("type")
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in data.items()}


# --- Conversations ----------------------------------------------------------------


async def create_conversation(db: AsyncSession, user: User) -> Conversation:
    conversation = Conversation(user_id=user.id, title="New conversation")
    db.add(conversation)
    await db.commit()
    return conversation


async def list_conversations(db: AsyncSession, user: User) -> list[Conversation]:
    return list(
        await db.scalars(
            select(Conversation)
            .where(Conversation.user_id == user.id)
            .order_by(Conversation.updated_at.desc())
        )
    )


async def get_conversation(
    db: AsyncSession, user: User, conversation_id: uuid.UUID
) -> Conversation | None:
    """The user's conversation, or None (also for other users' ids: no leaks)."""
    return await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user.id
        )
    )


async def load_messages(db: AsyncSession, conversation: Conversation) -> list[ChatMessage]:
    return list(
        await db.scalars(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation.id)
            .order_by(ChatMessage.seq)
        )
    )


def visible_text(message: ChatMessage) -> str:
    """The text a person should see: text blocks only (no thinking, no tools)."""
    return "".join(b.get("text", "") for b in message.content if b.get("type") == "text")


# --- One turn -------------------------------------------------------------------------


async def _lock(db: AsyncSession, conversation: Conversation) -> bool:
    """Lock the conversation for this turn; False if another turn holds it.

    NOWAIT fails immediately instead of queueing, so a double-submitted
    message gets a clear "busy" rather than a second, interleaved reply.
    """
    try:
        await db.execute(
            select(Conversation.id)
            .where(Conversation.id == conversation.id)
            .with_for_update(nowait=True)
        )
    except DBAPIError:
        await db.rollback()
        return False
    return True


async def stream_reply(
    db: AsyncSession,
    settings: Settings,
    model: ChatModel,
    access: ChatStatus,
    conversation: Conversation,
    text: str,
    tools: ToolContext,
) -> AsyncIterator[ChatEvent]:
    """Run one turn, yielding events for the client. Never raises for API errors."""
    if not await _lock(db, conversation):
        yield ErrorEvent("busy", "Still answering your previous message. Try again in a moment.")
        return

    history = await load_messages(db, conversation)
    # This turn's messages, in order, saved together at the end.
    turn: list[tuple[str, list[dict[str, Any]]]] = [("user", [{"type": "text", "text": text}])]
    tool_steps = 0
    answer_now = False  # set once the tool-step cap is reached
    total_cost = Decimal(0)

    async def give_up(code: str, message: str) -> AsyncIterator[ChatEvent]:
        # Keep the usage of calls already made; drop this turn's messages.
        await db.commit()
        yield ErrorEvent(code, message)
        yield DoneEvent(
            message_id=None, stop_reason=None, truncated=False, cost_usd=float(total_cost)
        )

    while True:
        if tool_steps > 0:
            status = await get_chat_status(db, settings, access.user)
            if status.denial is not None:
                async for event in give_up("budget", "Today's chat budget ran out mid-answer."):
                    yield event
                return

        messages = [cast(MessageParam, {"role": m.role, "content": m.content}) for m in history]
        messages += [cast(MessageParam, {"role": role, "content": c}) for role, c in turn]
        reply: ModelReply | None = None
        try:
            async for item in model.stream(
                model=settings.chat_model,
                system=SYSTEM_PROMPT,
                messages=messages,
                max_tokens=settings.chat_max_tokens,
                effort=settings.chat_effort,
                tools=TOOLS,
                tool_choice={"type": "none"} if answer_now else None,
            ):
                if isinstance(item, TextDelta):
                    yield TextEvent(item.text)
                else:
                    reply = item
        except ModelUnavailableError:
            reply = None
        if reply is None:
            async for event in give_up(
                "unavailable", "The assistant is unavailable right now. Please try again."
            ):
                yield event
            return

        usage_row = await record_usage(
            db,
            user=access.user,
            grant=access.grant,
            model=reply.model,
            usage=reply.usage,
            stop_reason=reply.stop_reason,
            conversation_id=conversation.id,
            commit=False,
        )
        total_cost += usage_row.cost_usd
        tool_uses = [b for b in reply.content if b.get("type") == "tool_use"]

        # Stop conditions that leave nothing valid to replay.
        if reply.stop_reason == "refusal" and (tool_uses or not reply.content):
            async for event in give_up("refused", "Sorry, I can't help with that request."):
                yield event
            return
        if tool_uses and (reply.stop_reason == "max_tokens" or answer_now):
            async for event in give_up(
                "truncated", "That answer got too long. Try asking something more specific."
            ):
                yield event
            return

        turn.append(("assistant", reply.content))
        if not tool_uses:
            break

        # Run every requested tool, then send all results back in one message.
        tool_steps += 1
        results: list[dict[str, Any]] = []
        for block in tool_uses:
            name, raw_input = str(block.get("name")), block.get("input")
            yield ToolStartEvent(name, raw_input if isinstance(raw_input, dict) else {})
            try:
                outcome = await run_tool(tools, name, raw_input)
            except Exception:  # a bug or DB failure in a tool must not kill the stream
                logger.exception("Tool %s failed", name)
                outcome = ToolOutcome(
                    '{"error": "The tool failed."}', is_error=True, summary="Failed"
                )
            yield ToolResultEvent(name, not outcome.is_error, outcome.summary, outcome.places)
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": block.get("id"),
                "content": outcome.content,
            }
            if outcome.is_error:
                result["is_error"] = True
            results.append(result)
        turn.append(("user", results))
        if tool_steps >= settings.chat_max_tool_steps:
            answer_now = True  # one more call, with tools disabled

    # Save the whole turn.
    seq = len(history)
    rows = [
        ChatMessage(conversation_id=conversation.id, seq=seq + i, role=role, content=content)
        for i, (role, content) in enumerate(turn)
    ]
    db.add_all(rows)
    await db.flush()
    if not history:
        conversation.title = text.strip()[:TITLE_LENGTH] or conversation.title
    conversation.updated_at = datetime.now(UTC)
    await db.commit()

    if reply.stop_reason == "refusal":
        yield ErrorEvent("refused", "Sorry, I can't help with that request.")
    yield DoneEvent(
        message_id=rows[-1].id,
        stop_reason=reply.stop_reason,
        truncated=reply.stop_reason == "max_tokens",
        cost_usd=float(total_cost),
    )
