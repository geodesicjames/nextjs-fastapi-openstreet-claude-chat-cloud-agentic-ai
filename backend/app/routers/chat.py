"""Chat routes: access status, conversations, and streamed replies.

Every route that calls Claude must depend on `ChatAccess` (see app/deps.py),
which enforces sign-in, the invite-only grant and the daily budget.

Replies stream as server-sent events (SSE): a `text/event-stream` response
made of `event: <type>` / `data: <json>` pairs. Event types: `text` (a
chunk of the reply), `tool_start` / `tool_result` (what the assistant is
looking up, with places to show on the map), `error` (`code`: refused |
unavailable | busy | budget | truncated) and `done` (stop reason, truncated
flag, total cost of the turn). SSE over a POST is read with
fetch() streaming in the browser; EventSource only supports GET.
"""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.config import Settings, get_settings
from app.deps import ChatAccess, CurrentUser, DbSession, StreamingDbSession, get_chat_model
from app.schemas.chat import (
    ChatAccessOut,
    ConversationDetail,
    ConversationOut,
    MessageIn,
    MessageOut,
)
from app.services import chat as chat_service
from app.services.chat_access import get_chat_status
from app.services.chat_tools import ToolContext
from app.services.llm import ChatModel

SettingsDep = Annotated[Settings, Depends(get_settings)]

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/access", response_model=ChatAccessOut)
async def chat_access(
    db: DbSession, user: CurrentUser, settings: Annotated[Settings, Depends(get_settings)]
) -> ChatAccessOut:
    """The caller's chat access and today's usage.

    Needs a signed-in user but *not* chat access: someone without a grant gets
    `allowed: false` and a reason, rather than a 403.
    """
    chat_status = await get_chat_status(db, settings, user)
    return ChatAccessOut.from_status(chat_status, settings.chat_mode)


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(db: DbSession, user: CurrentUser) -> list[ConversationOut]:
    """The signed-in user's conversations, most recently active first."""
    conversations = await chat_service.list_conversations(db, user)
    return [ConversationOut.model_validate(c) for c in conversations]


@router.post("/conversations", status_code=status.HTTP_201_CREATED, response_model=ConversationOut)
async def create_conversation(db: DbSession, access: ChatAccess) -> ConversationOut:
    conversation = await chat_service.create_conversation(db, access.user)
    return ConversationOut.model_validate(conversation)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> ConversationDetail:
    conversation = await chat_service.get_conversation(db, user, conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    messages = await chat_service.load_messages(db, conversation)
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,  # pyright: ignore[reportArgumentType]  # "user" | "assistant" in the DB
                text=chat_service.visible_text(m),
                created_at=m.created_at,
            )
            for m in messages
            # Tool calls and results have no text to show.
            if chat_service.visible_text(m)
        ],
    )


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageIn,
    request: Request,
    db: StreamingDbSession,
    access: ChatAccess,
    settings: SettingsDep,
    model: Annotated[ChatModel, Depends(get_chat_model)],
) -> StreamingResponse:
    """Send a message and stream Claude's reply as server-sent events."""
    if len(body.text) > settings.chat_max_message_chars:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Messages are limited to {settings.chat_max_message_chars} characters.",
        )
    conversation = await chat_service.get_conversation(db, access.user, conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")

    tools = ToolContext(
        db=db,
        settings=settings,
        user=access.user,
        overpass=request.app.state.overpass,
        nominatim=request.app.state.nominatim,
    )

    async def events() -> AsyncIterator[str]:
        async for event in chat_service.stream_reply(
            db, settings, model, access, conversation, body.text, tools, body.map
        ):
            yield f"event: {event.type}\ndata: {json.dumps(chat_service.event_payload(event))}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # Don't let proxies buffer or cache the stream.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
