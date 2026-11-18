"""Schemas for access requests and admin grant management."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field

from app.models import AccessRequestStatus

# Shared field rules. AwareDatetime rejects timestamps without a timezone,
# which would otherwise be ambiguous ("expires at 17:00" where?).
# A daily budget in USD: positive, at most $100, whole cents.
BudgetUsd = Field(default=None, gt=0, le=100, decimal_places=2)
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
    # null: use the CHAT_DAILY_BUDGET_USD setting.
    daily_budget_usd: Decimal | None = BudgetUsd
    expires_at: AwareDatetime | None = None


class GrantUpdate(BaseModel):
    """PATCH body. Only fields that are sent are changed (see update_grant)."""

    note: str = Note
    daily_budget_usd: Decimal | None = BudgetUsd
    expires_at: AwareDatetime | None = None


class GrantOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_email: str
    note: str
    # The grant's own budget (USD); null means it follows the default setting.
    daily_budget_usd: float | None
    # The budget actually applied (own budget, or the current default).
    effective_daily_budget_usd: float
    spent_today_usd: float
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
