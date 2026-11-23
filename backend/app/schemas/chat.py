"""Response schemas for chat access."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.chat_access import ChatStatus, Denial


class ChatAccessOut(BaseModel):
    """What the frontend needs to show chat as available, or explain why not."""

    allowed: bool
    # Why chat is unavailable; null when allowed.
    reason: Denial | None
    mode: Literal["invite_only", "open"]
    # USD, rounded to cents for display.
    daily_budget_usd: float
    spent_today_usd: float
    remaining_usd: float
    resets_at: datetime
    grant_expires_at: datetime | None

    @classmethod
    def from_status(cls, s: ChatStatus, mode: Literal["invite_only", "open"]) -> "ChatAccessOut":
        return cls(
            allowed=s.allowed,
            reason=s.denial,
            mode=mode,
            daily_budget_usd=round(float(s.daily_budget_usd), 2),
            spent_today_usd=round(float(s.spent_today_usd), 4),
            remaining_usd=round(float(s.remaining_usd), 4),
            resets_at=s.resets_at,
            grant_expires_at=s.grant.expires_at if s.grant else None,
        )


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ChatPlaceOut(BaseModel):
    """A place a tool found while answering (the tool_result event's places)."""

    place_id: uuid.UUID
    name: str | None
    category: str
    lat: float
    lon: float


class MessageOut(BaseModel):
    """A message as people see it: its text (no thinking or tool blocks), and
    for an answer, the places found while writing it."""

    id: uuid.UUID
    role: Literal["user", "assistant"]
    text: str
    created_at: datetime
    places: list[ChatPlaceOut] = []


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class MapView(BaseModel):
    """What the user's map shows as they send a message, so "near here" and a
    bare place name mean the area they're looking at, not the whole world."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # From the centre to the view's farthest corner.
    radius_m: int = Field(ge=1, le=20_000_000)
    # The place whose card is open, if any. Only the id is taken from the
    # browser; its name and category are looked up in the database.
    selected_place_id: uuid.UUID | None = None


class MessageIn(BaseModel):
    # The upper bound is CHAT_MAX_MESSAGE_CHARS, checked in the endpoint so it
    # follows the setting.
    text: str = Field(min_length=1)
    map: MapView | None = None
