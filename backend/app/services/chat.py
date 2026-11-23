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

The one exception is deleting a turn (`delete_turn`), when a user wants the
assistant to forget a question. Each thinking block's signature is bound to
the conversation before it, so removing a turn from the middle would make
the API reject every later thinking block. Deleting a turn therefore also
strips the thinking blocks from the rest of the conversation, which the API
accepts. The visible answers stay; the next reply just can't see the
assistant's earlier reasoning, and the prompt cache restarts once.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast

from anthropic.types import MessageParam
from sqlalchemy import delete, exists, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ChatMessage, Conversation, Place, User
from app.schemas.chat import MapView
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
- A user message may start with a <map_view> note. The app adds it, not the \
user: where their map is centred, how far the view reaches, and the place \
they have selected, if any. Unless the user names somewhere else, that view \
is where they mean. For "near here", or a business or kind of place with no \
town or area named, call search_places around the map's centre (radius \
about the view's size, at most 2000) rather than find_location, which \
searches the whole world. If they ask about the selected place, call \
get_place_details with its place_id directly.
- When the user names a town, area, street or landmark, call find_location \
first, then search_places around the best match. If the name is ambiguous, \
prefer a match near the map view, otherwise the likeliest one, and say \
which one you used.
- Only recommend places that search_places returned. Never invent \
businesses, addresses, opening hours or prices. If a search finds nothing, \
say so and suggest a larger radius or another category.
- Describe places only with what the tools returned: name, category, \
distance, and the tags from get_place_details. People act on what you say \
(walking somewhere, relying on accessibility), so don't add details the \
data doesn't contain: no guesses from a place's name ("Riverside Café is \
by the river"), no impressions ("cosy", "popular", "spacious"), and no positions \
relative to landmarks the results don't mention. Distances, and directions \
worked out from coordinates, are fine. For "how many", use the total_found \
number search_places returns, or say "several"; don't count a list by hand. \
When you list only some of the results, say so.
- Read tags literally and don't extend their meaning: a tag saying a \
facility exists for one group doesn't mean others are excluded, and a \
missing tag means the data doesn't say, not "no". If you're unsure what a \
tag means, quote it as written.
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
    # The question's id, for deleting this turn later; None if nothing was saved.
    user_message_id: uuid.UUID | None
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
    """The user's conversations with at least one saved message.

    An empty one (created, but its first message failed) isn't worth a row
    in the list; the browser that created it can still send to it.
    """
    has_messages = exists().where(ChatMessage.conversation_id == Conversation.id)
    return list(
        await db.scalars(
            select(Conversation)
            .where(Conversation.user_id == user.id, has_messages)
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
    """The text a person should see: text blocks only (no thinking, no tools,
    no map-view notes the app added to the user's message)."""
    return "".join(
        b.get("text", "")
        for b in message.content
        if b.get("type") == "text" and not b.get("text", "").startswith(MAP_VIEW_TAG)
    )


def is_question(message: ChatMessage) -> bool:
    """A message the user typed (as opposed to tool results, also sent as "user")."""
    return message.role == "user" and bool(visible_text(message))


PLACE_FIELDS = ("place_id", "name", "category", "lat", "lon")


def _places_in_result(block: dict[str, Any]) -> list[dict[str, Any]]:
    """The places in a stored tool_result: search_places' list, or
    get_place_details' one place. Anything else (errors, other tools) has none."""
    if block.get("type") != "tool_result" or block.get("is_error"):
        return []
    try:
        data = json.loads(block.get("content") or "")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    listed = data.get("places")
    candidates: list[Any] = listed if isinstance(listed, list) else [data]
    return [
        {k: p[k] for k in PLACE_FIELDS}
        for p in candidates
        if isinstance(p, dict) and all(k in p for k in PLACE_FIELDS)
    ]


def places_by_answer(messages: list[ChatMessage]) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """For each answer (an assistant message with text), the places its turn's
    tools found before it, so names in a reloaded answer can still link to them."""
    out: dict[uuid.UUID, list[dict[str, Any]]] = {}
    found: dict[str, dict[str, Any]] = {}  # by place_id, in the order found
    for message in messages:
        if is_question(message):
            found = {}
        elif message.role == "user":
            for block in message.content:
                for place in _places_in_result(block):
                    found.setdefault(str(place["place_id"]), place)
        elif visible_text(message) and found:
            out[message.id] = list(found.values())
    return out


THINKING_BLOCKS = {"thinking", "redacted_thinking"}


async def delete_conversation(
    db: AsyncSession, conversation: Conversation
) -> Literal["deleted", "busy"]:
    """Delete a conversation and all its messages.

    "busy" while a reply is being written. The messages go with it (ON DELETE
    CASCADE in the database); the usage rows stay, with no conversation, so
    the day's spend still counts.
    """
    if not await _lock(db, conversation):
        return "busy"
    await db.execute(delete(Conversation).where(Conversation.id == conversation.id))
    await db.commit()
    return "deleted"


async def delete_turn(
    db: AsyncSession, conversation: Conversation, message_id: uuid.UUID
) -> Literal["deleted", "not_found", "busy"]:
    """Delete one question and everything the assistant did to answer it.

    Deleting the last question deletes the conversation too, rather than
    leaving an empty one behind. "not_found" if `message_id` isn't one of
    this conversation's questions; "busy" while a reply is being written
    (the turn holds the lock).
    """
    if not await _lock(db, conversation):
        return "busy"
    messages = await load_messages(db, conversation)
    start = next((i for i, m in enumerate(messages) if m.id == message_id and is_question(m)), None)
    if start is None:
        await db.rollback()  # release the lock
        return "not_found"
    end = next(
        (i for i in range(start + 1, len(messages)) if is_question(messages[i])), len(messages)
    )
    remaining = messages[:start] + messages[end:]
    if not any(is_question(m) for m in remaining):
        await db.execute(delete(Conversation).where(Conversation.id == conversation.id))
        await db.commit()
        return "deleted"

    for message in messages[start:end]:
        await db.delete(message)
    # See the module docstring: without this, the next reply would fail.
    for message in remaining:
        if message.role == "assistant" and any(
            b.get("type") in THINKING_BLOCKS for b in message.content
        ):
            message.content = [b for b in message.content if b.get("type") not in THINKING_BLOCKS]

    # The title is the first question: don't keep showing a deleted one.
    first = next(m for m in remaining if is_question(m))
    conversation.title = visible_text(first).strip()[:TITLE_LENGTH] or "New conversation"
    await db.commit()
    return "deleted"


# --- Map view ---------------------------------------------------------------------------

MAP_VIEW_TAG = "<map_view>"


async def describe_map_view(db: AsyncSession, view: MapView) -> str:
    """The note added before the user's message: what their map shows.

    It goes in the user's message, not the system prompt, so the system
    prompt stays identical (and cached) whatever the map shows. It's saved
    with the message, so a replayed conversation keeps the view each message
    was asked in. The selected place's name comes from the database, never
    from the browser.
    """
    reach = f"{view.radius_m / 1000:.1f} km" if view.radius_m >= 1000 else f"{view.radius_m} m"
    lines = [
        f"The user's map is centred on {view.lat:.5f}, {view.lon:.5f} "
        f"and reaches about {reach} from the centre."
    ]
    place = await db.get(Place, view.selected_place_id) if view.selected_place_id else None
    if place is not None:
        name = place.name or "an unnamed place"
        lines.append(
            f"They have selected {name} ({place.category.replace('_', ' ')}, place_id {place.id})."
        )
    return f"{MAP_VIEW_TAG}\n" + "\n".join(lines) + "\n</map_view>"


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
    map_view: MapView | None = None,
) -> AsyncIterator[ChatEvent]:
    """Run one turn, yielding events for the client. Never raises for API errors."""
    if not await _lock(db, conversation):
        yield ErrorEvent("busy", "Still answering your previous message. Try again in a moment.")
        return

    history = await load_messages(db, conversation)
    user_content: list[dict[str, Any]] = []
    if map_view is not None:
        user_content.append({"type": "text", "text": await describe_map_view(db, map_view)})
    user_content.append({"type": "text", "text": text})
    # This turn's messages, in order, saved together at the end.
    turn: list[tuple[str, list[dict[str, Any]]]] = [("user", user_content)]
    tool_steps = 0
    answer_now = False  # set once the tool-step cap is reached
    total_cost = Decimal(0)

    async def give_up(code: str, message: str) -> AsyncIterator[ChatEvent]:
        # Keep the usage of calls already made; drop this turn's messages.
        await db.commit()
        yield ErrorEvent(code, message)
        yield DoneEvent(
            message_id=None,
            user_message_id=None,
            stop_reason=None,
            truncated=False,
            cost_usd=float(total_cost),
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

    # Save the whole turn. After the last stored row, not at len(history):
    # deleted turns leave gaps in seq.
    seq = history[-1].seq + 1 if history else 0
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
        user_message_id=rows[0].id,
        stop_reason=reply.stop_reason,
        truncated=reply.stop_reason == "max_tokens",
        cost_usd=float(total_cost),
    )
