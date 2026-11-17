"""Schemas for access requests and admin grant management."""

import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field

from app.models import AccessRequestStatus

# Shared field rules. AwareDatetime rejects timestamps without a timezone,
# which would otherwise be ambiguous ("expires at 17:00" where?).
TokenLimit = Field(default=None, gt=0, le=10_000_000)
Note = Field(default="", max_length=500)


class AccessRequestIn(BaseModel):
    email: EmailStr
    message: str = Field(default="", max_length=1000)


class AccessRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    message: str
    status: AccessRequestStatus
    created_at: datetime
    decided_at: datetime | None


class ApproveIn(BaseModel):
    """Grant settings chosen when approving. All optional."""

    note: str = Note
    # null: use the CHAT_DEFAULT_DAILY_TOKENS setting.
    daily_token_limit: int | None = TokenLimit
    expires_at: AwareDatetime | None = None


class GrantUpdate(BaseModel):
    """PATCH body. Only fields that are sent are changed (see update_grant)."""

    note: str = Note
    daily_token_limit: int | None = TokenLimit
    expires_at: AwareDatetime | None = None


class GrantOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_email: str
    note: str
    # The grant's own limit; null means it follows the default setting.
    daily_token_limit: int | None
    # The limit actually applied (own limit, or the current default).
    effective_daily_token_limit: int
    tokens_used_today: int
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
