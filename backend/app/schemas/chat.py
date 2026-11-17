"""Response schemas for chat access."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.services.chat_access import ChatStatus, Denial


class ChatAccessOut(BaseModel):
    """What the frontend needs to show chat as available, or explain why not."""

    allowed: bool
    # Why chat is unavailable; null when allowed.
    reason: Denial | None
    mode: Literal["invite_only", "open"]
    daily_token_limit: int
    tokens_used_today: int
    tokens_remaining: int
    resets_at: datetime
    grant_expires_at: datetime | None

    @classmethod
    def from_status(cls, s: ChatStatus, mode: Literal["invite_only", "open"]) -> "ChatAccessOut":
        return cls(
            allowed=s.allowed,
            reason=s.denial,
            mode=mode,
            daily_token_limit=s.daily_limit,
            tokens_used_today=s.used_today,
            tokens_remaining=s.remaining,
            resets_at=s.resets_at,
            grant_expires_at=s.grant.expires_at if s.grant else None,
        )
