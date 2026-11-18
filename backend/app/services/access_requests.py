"""Access requests and grant administration.

Flow: someone submits the public form -> admins are emailed -> an admin
approves (creating the user if needed, plus a grant) or denies -> on approval
the requester is emailed a sign-in link.

Routers handle HTTP and send the emails; this module holds the rules.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AccessRequest, AccessRequestStatus, ChatGrant, LlmUsage, User
from app.services.auth import normalize_email
from app.services.chat_access import create_grant, get_or_create_user, start_of_day


class NotPendingError(Exception):
    """The request has already been approved or denied.

    The router checks this up front for a friendly 409; this is the backstop.
    """


class GrantRevokedError(Exception):
    """Revoked grants are history and can't be edited."""


def _now() -> datetime:
    return datetime.now(UTC)


async def create_access_request(
    db: AsyncSession, settings: Settings, email: str, message: str
) -> AccessRequest | None:
    """Store a request, or return None if this address is over its daily limit."""
    email = normalize_email(email)
    recent = await db.scalar(
        select(func.count())
        .select_from(AccessRequest)
        .where(
            AccessRequest.email == email,
            AccessRequest.created_at > _now() - timedelta(days=1),
        )
    )
    if (recent or 0) >= settings.access_request_max_per_day:
        return None

    request = AccessRequest(email=email, message=message.strip())
    db.add(request)
    await db.commit()
    return request


async def admin_emails(db: AsyncSession) -> list[str]:
    return list(await db.scalars(select(User.email).where(User.is_admin).order_by(User.email)))


async def list_access_requests(
    db: AsyncSession, status: AccessRequestStatus | None
) -> list[AccessRequest]:
    query = select(AccessRequest).order_by(AccessRequest.created_at.desc())
    if status is not None:
        query = query.where(AccessRequest.status == status)
    return list(await db.scalars(query))


async def get_access_request(db: AsyncSession, request_id: uuid.UUID) -> AccessRequest | None:
    # FOR UPDATE: two admins clicking approve at once are serialized, and the
    # second sees the request is no longer pending.
    return await db.scalar(
        select(AccessRequest).where(AccessRequest.id == request_id).with_for_update()
    )


def _decide(request: AccessRequest, admin: User, status: AccessRequestStatus) -> None:
    if request.status != AccessRequestStatus.PENDING:
        raise NotPendingError
    request.status = status
    request.decided_by_id = admin.id
    request.decided_at = _now()


async def approve_access_request(
    db: AsyncSession,
    request: AccessRequest,
    admin: User,
    *,
    note: str,
    daily_budget_usd: Decimal | None,
    expires_at: datetime | None,
) -> ChatGrant:
    """Approve: create the user if needed and give them a grant.

    Raises NotPendingError, or GrantConflictError if they already have an
    active grant (nothing is changed in either case).
    """
    _decide(request, admin, AccessRequestStatus.APPROVED)
    user = await get_or_create_user(db, request.email)
    grant = await create_grant(
        db,
        user=user,
        granted_by=admin,
        note=note,
        daily_budget_usd=daily_budget_usd,
        expires_at=expires_at,
    )
    await db.commit()
    return grant


async def deny_access_request(db: AsyncSession, request: AccessRequest, admin: User) -> None:
    _decide(request, admin, AccessRequestStatus.DENIED)
    await db.commit()


# --- Grants ---------------------------------------------------------------------


async def list_grants(db: AsyncSession, *, include_revoked: bool) -> list[ChatGrant]:
    query = select(ChatGrant).order_by(ChatGrant.created_at.desc())
    if not include_revoked:
        query = query.where(ChatGrant.revoked_at.is_(None))
    return list(await db.scalars(query))


async def spent_today_by_user(
    db: AsyncSession, user_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Today's spend (USD) for many users in one query (for the admin list)."""
    if not user_ids:
        return {}
    rows = await db.execute(
        select(LlmUsage.user_id, func.sum(LlmUsage.cost_usd))
        .where(LlmUsage.user_id.in_(user_ids), LlmUsage.created_at >= start_of_day(_now()))
        .group_by(LlmUsage.user_id)
    )
    return {user_id: Decimal(total) for user_id, total in rows}


async def get_grant(db: AsyncSession, grant_id: uuid.UUID) -> ChatGrant | None:
    return await db.get(ChatGrant, grant_id)


async def update_grant(db: AsyncSession, grant: ChatGrant, changes: dict[str, object]) -> None:
    """Apply admin edits (daily_budget_usd, expires_at, note).

    `changes` holds only the fields the admin sent, so sending
    `daily_budget_usd: null` (back to the default) differs from leaving it out.
    """
    if grant.revoked_at is not None:
        raise GrantRevokedError
    for field, value in changes.items():
        setattr(grant, field, value)
    await db.commit()


async def revoke_grant(db: AsyncSession, grant: ChatGrant) -> None:
    """Revoke a grant. Idempotent: revoking twice keeps the first time."""
    if grant.revoked_at is None:
        grant.revoked_at = _now()
        await db.commit()
