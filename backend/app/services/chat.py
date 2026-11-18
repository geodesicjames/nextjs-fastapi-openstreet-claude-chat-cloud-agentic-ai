"""Conversations with Claude: history, one streamed reply per user message.

A turn:
1. Lock the conversation row (one reply at a time per conversation).
2. Replay the stored history plus the new user message to the model.
3. Stream text to the caller as it arrives.
4. Save the user message, the assistant reply (all its content blocks,
   unchanged) and the call's usage/cost in a single commit.

If the call fails, nothing is saved: the user sees an error and can resend,
and the history never holds a user message without its reply. History is
append-only: rows are never edited, so the replayed prefix stays byte-for-
byte identical (required for the model's replayed thinking blocks, and what
makes prompt caching hit).

Tool use arrives in the next PR; this module already yields the event types
the frontend consumes.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, cast

from anthropic.types import MessageParam
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ChatMessage, Conversation, User
from app.services.chat_access import ChatStatus, record_usage
from app.services.llm import ChatModel, ModelReply, ModelUnavailableError, TextDelta

logger = logging.getLogger(__name__)

# Kept byte-for-byte stable (no dates, no per-user text) so it caches.
SYSTEM_PROMPT = """\
You are the assistant inside "Places", a web app with an OpenStreetMap-based \
map for finding places such as cafés, restaurants, parks, toilets, drinking \
water and viewpoints.

Help people decide where to go: answer questions about places and areas, \
suggest what to look for, and explain how to use the map.

You don't have live access to the map's data in this version. Don't invent \
specific businesses, addresses, opening hours or prices; say you can't look \
them up yet and suggest searching on the map instead.

Keep answers short and practical. Use plain language and simple Markdown \
(short lists are fine; no tables)."""

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
    # refused | unavailable | busy
    code: str
    message: str
    type: str = "error"


ChatEvent = TextEvent | DoneEvent | ErrorEvent


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
) -> AsyncIterator[ChatEvent]:
    """Run one turn, yielding events for the client. Never raises for API errors."""
    if not await _lock(db, conversation):
        yield ErrorEvent("busy", "Still answering your previous message. Try again in a moment.")
        return

    history = await load_messages(db, conversation)
    user_content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    messages = [cast(MessageParam, {"role": m.role, "content": m.content}) for m in history]
    messages.append(cast(MessageParam, {"role": "user", "content": user_content}))

    reply: ModelReply | None = None
    try:
        async for item in model.stream(
            model=settings.chat_model,
            system=SYSTEM_PROMPT,
            messages=messages,
            max_tokens=settings.chat_max_tokens,
            effort=settings.chat_effort,
        ):
            if isinstance(item, TextDelta):
                yield TextEvent(item.text)
            else:
                reply = item
    except ModelUnavailableError:
        await db.rollback()  # releases the lock; nothing was saved
        yield ErrorEvent("unavailable", "The assistant is unavailable right now. Please try again.")
        return
    if reply is None:  # a ChatModel must end with a reply; treat as unavailable
        await db.rollback()
        yield ErrorEvent("unavailable", "The assistant is unavailable right now. Please try again.")
        return

    # A refusal can arrive with no content at all. The API rejects empty
    # assistant turns on replay, so such a turn isn't added to the history
    # (there's nothing in it to preserve); its cost is still logged.
    message_id: uuid.UUID | None = None
    if reply.content:
        seq = len(history)
        db.add(
            ChatMessage(conversation_id=conversation.id, seq=seq, role="user", content=user_content)
        )
        assistant = ChatMessage(
            conversation_id=conversation.id, seq=seq + 1, role="assistant", content=reply.content
        )
        db.add(assistant)
        await db.flush()
        message_id = assistant.id
        if not history:
            conversation.title = text.strip()[:TITLE_LENGTH] or conversation.title
        conversation.updated_at = datetime.now(UTC)

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
    await db.commit()

    if reply.stop_reason == "refusal":
        yield ErrorEvent("refused", "Sorry, I can't help with that request.")
    yield DoneEvent(
        message_id=message_id,
        stop_reason=reply.stop_reason,
        truncated=reply.stop_reason == "max_tokens",
        cost_usd=float(usage_row.cost_usd),
    )
