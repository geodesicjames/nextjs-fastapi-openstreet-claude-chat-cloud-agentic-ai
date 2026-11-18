"""Who may use chat, and how much of today's budget is left.

The rules, in order:
1. invite_only mode: the user needs an active grant (not revoked, not
   expired). open mode: any signed-in user may chat; a grant, if they have
   one, still sets their limit.
2. Today's spend (UTC day, the sum of `llm_usage.cost_usd`) must be below
   the daily budget in USD.

The budget is checked *before* each Claude call, so a call that starts just
under the budget can finish a little over it. That overshoot is bounded by
the call's `max_tokens`, which every call must set (on Haiku 5.5 a full
16K-token reply costs under a cent).

`app/deps.py` turns a denied status into an HTTP error; `GET /chat/access`
returns the status as-is so the frontend can explain it.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ChatGrant, LlmUsage, User
from app.services.auth import normalize_email
from app.services.pricing import TokenUsage, cost_usd


class Denial(StrEnum):
    NO_GRANT = "no_grant"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class ChatStatus:
    user: User
    grant: ChatGrant | None
    daily_budget_usd: Decimal
    spent_today_usd: Decimal
    resets_at: datetime
    denial: Denial | None

    @property
    def allowed(self) -> bool:
        return self.denial is None

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.daily_budget_usd - self.spent_today_usd, Decimal(0))


def _now() -> datetime:
    return datetime.now(UTC)


def start_of_day(now: datetime) -> datetime:
    """Midnight UTC at the start of `now`'s day. Budgets reset here."""
    return datetime.combine(now.astimezone(UTC).date(), time.min, tzinfo=UTC)


async def get_active_grant(db: AsyncSession, user: User, now: datetime) -> ChatGrant | None:
    return await db.scalar(
        select(ChatGrant).where(
            ChatGrant.user_id == user.id,
            ChatGrant.revoked_at.is_(None),
            or_(ChatGrant.expires_at.is_(None), ChatGrant.expires_at > now),
        )
    )


async def spent_since(db: AsyncSession, user: User, since: datetime) -> Decimal:
    total = await db.scalar(
        select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)).where(
            LlmUsage.user_id == user.id, LlmUsage.created_at >= since
        )
    )
    return Decimal(total or 0)


async def get_chat_status(
    db: AsyncSession, settings: Settings, user: User, now: datetime | None = None
) -> ChatStatus:
    now = now or _now()
    day_start = start_of_day(now)
    grant = await get_active_grant(db, user, now)

    budget = settings.chat_daily_budget_usd
    if grant is not None and grant.daily_budget_usd is not None:
        budget = grant.daily_budget_usd

    spent = await spent_since(db, user, day_start)

    denial: Denial | None = None
    if grant is None and settings.chat_mode == "invite_only":
        denial = Denial.NO_GRANT
    elif spent >= budget:
        denial = Denial.BUDGET_EXHAUSTED

    return ChatStatus(
        user=user,
        grant=grant,
        daily_budget_usd=budget,
        spent_today_usd=spent,
        resets_at=day_start + timedelta(days=1),
        denial=denial,
    )


async def record_usage(
    db: AsyncSession,
    *,
    user: User,
    grant: ChatGrant | None,
    model: str,
    usage: TokenUsage,
    stop_reason: str | None = None,
    conversation_id: uuid.UUID | None = None,
    commit: bool = True,
) -> LlmUsage:
    """Log one Claude call with its estimated cost (from app/services/pricing)."""
    row = LlmUsage(
        user_id=user.id,
        grant_id=grant.id if grant else None,
        conversation_id=conversation_id,
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        stop_reason=stop_reason,
        cost_usd=cost_usd(model, usage),
    )
    db.add(row)
    # The chat service saves the turn and its usage in one commit.
    if commit:
        await db.commit()
    return row


class GrantConflictError(Exception):
    """The user already has an active grant; edit that one instead."""


async def get_or_create_user(db: AsyncSession, email: str) -> User:
    """Find a user by email, creating them if needed. The caller commits.

    Approving someone who has never signed in creates their account up front,
    so the grant has a user to point at. Their first sign-in then finds it.
    """
    email = normalize_email(email)
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email)
        db.add(user)
        await db.flush()  # assigns user.id without committing
    return user


async def create_grant(
    db: AsyncSession,
    *,
    user: User,
    granted_by: User | None,
    note: str = "",
    daily_budget_usd: Decimal | None = None,
    expires_at: datetime | None = None,
) -> ChatGrant:
    """Add a grant for `user`. The caller commits.

    Raises GrantConflictError if the user already has an active grant.
    """
    now = _now()
    # The unique index counts expired-but-unrevoked grants as active, so an
    # expired grant has to be revoked before a new one can be added.
    existing = await db.scalar(
        select(ChatGrant).where(ChatGrant.user_id == user.id, ChatGrant.revoked_at.is_(None))
    )
    if existing is not None:
        if existing.expires_at is None or existing.expires_at > now:
            raise GrantConflictError
        existing.revoked_at = now
        await db.flush()

    # Pass the User object, not just user_id, so `grant.user` is already
    # loaded: async SQLAlchemy can't lazy-load it on first access later.
    grant = ChatGrant(
        user=user,
        granted_by_id=granted_by.id if granted_by else None,
        note=note,
        daily_budget_usd=daily_budget_usd,
        expires_at=expires_at,
    )
    db.add(grant)
    await db.flush()
    return grant


async def make_admin(db: AsyncSession, email: str) -> tuple[User, ChatGrant]:
    """Make `email` an admin with an active chat grant (`createsuperuser`).

    Creates the user if they have never signed in. Safe to run twice.
    """
    user = await get_or_create_user(db, email)
    user.is_admin = True

    grant = await get_active_grant(db, user, _now())
    if grant is None:
        grant = await create_grant(db, user=user, granted_by=user, note="Owner (make-admin)")

    await db.commit()
    return user, grant
