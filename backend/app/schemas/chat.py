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


class MessageOut(BaseModel):
    """A message as people see it: just its text (no thinking or tool blocks)."""

    id: uuid.UUID
    role: Literal["user", "assistant"]
    text: str
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class MessageIn(BaseModel):
    # The upper bound is CHAT_MAX_MESSAGE_CHARS, checked in the endpoint so it
    # follows the setting.
    text: str = Field(min_length=1)
