"""Admin endpoints: review access requests, manage chat grants, see Claude spend,
and read agent runs step by step.

Every route depends on `AdminUser` (401 if signed out, 403 if not an admin).
"""

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.config import Settings, get_settings
from app.deps import AdminUser, DbSession
from app.models import AccessRequest, AccessRequestStatus, AgentRun, ChatGrant, User
from app.schemas.access import AccessRequestOut, ApproveIn, GrantOut, GrantUpdate
from app.schemas.agent import AdminRunOut, RunTrace
from app.schemas.usage import UsageReportOut
from app.services import access_requests as service
from app.services import agent as agent_service
from app.services import auth as auth_service
from app.services.chat_access import GrantConflictError
from app.services.email import EmailSender, get_email_sender
from app.services.usage_report import usage_report

router = APIRouter(prefix="/admin", tags=["admin"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _grant_out(grant: ChatGrant, settings: Settings, spent_today: Decimal) -> GrantOut:
    budget = grant.daily_budget_usd or settings.chat_daily_budget_usd
    return GrantOut(
        id=grant.id,
        user_id=grant.user_id,
        user_email=grant.user.email,
        note=grant.note,
        daily_budget_usd=(
            float(grant.daily_budget_usd) if grant.daily_budget_usd is not None else None
        ),
        effective_daily_budget_usd=float(budget),
        spent_today_usd=round(float(spent_today), 4),
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        created_at=grant.created_at,
    )


async def _grant_out_with_usage(db: DbSession, grant: ChatGrant, settings: Settings) -> GrantOut:
    spent = await service.spent_today_by_user(db, [grant.user_id])
    return _grant_out(grant, settings, spent.get(grant.user_id, Decimal(0)))


# --- Access requests --------------------------------------------------------------


@router.get("/access-requests", response_model=list[AccessRequestOut])
async def list_access_requests(
    db: DbSession,
    admin: AdminUser,
    status: AccessRequestStatus | None = AccessRequestStatus.PENDING,
) -> list[AccessRequestOut]:
    """Requests, newest first. Defaults to pending; pass ?status= to change."""
    requests = await service.list_access_requests(db, status)
    return [AccessRequestOut.model_validate(r) for r in requests]


async def _pending_request(db: DbSession, request_id: uuid.UUID) -> AccessRequest:
    """The request, locked for update; 404 if missing, 409 if already decided."""
    request = await service.get_access_request(db, request_id)
    if request is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Access request not found.")
    if request.status != AccessRequestStatus.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, f"This request was already {request.status}.")
    return request


@router.post("/access-requests/{request_id}/approve", response_model=GrantOut)
async def approve_access_request(
    request_id: uuid.UUID,
    body: ApproveIn,
    db: DbSession,
    admin: AdminUser,
    settings: SettingsDep,
    background: BackgroundTasks,
    email_sender: Annotated[EmailSender, Depends(get_email_sender)],
) -> GrantOut:
    """Approve: grant chat access and email the requester a sign-in link."""
    request = await _pending_request(db, request_id)
    try:
        grant = await service.approve_access_request(
            db,
            request,
            admin,
            note=body.note,
            daily_budget_usd=body.daily_budget_usd,
            expires_at=body.expires_at,
        )
    except GrantConflictError:
        # Undo the request's status change; nothing is saved.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That user already has an active grant. Edit it instead.",
        ) from None

    # Sign-in links expire quickly and approval emails may be read later, so
    # the email also says how to get a fresh link.
    login_url = f"{settings.frontend_url}/login"
    token = await auth_service.create_login_token(db, settings, request.email)
    link_text = (
        f"Sign in here (link valid for {settings.login_link_lifetime_minutes} minutes):\n"
        f"{settings.frontend_url}/auth/verify?token={token}\n\n"
        f"After that, sign in at {login_url} with this address."
        if token
        else f"Sign in at {login_url} with this address."
    )
    background.add_task(
        email_sender.send,
        to=request.email,
        subject="You have chat access",
        body=f"Your request for chat access was approved.\n\n{link_text}",
    )
    return await _grant_out_with_usage(db, grant, settings)


@router.post("/access-requests/{request_id}/deny", status_code=status.HTTP_204_NO_CONTENT)
async def deny_access_request(request_id: uuid.UUID, db: DbSession, admin: AdminUser) -> None:
    """Deny a request. The requester is not emailed."""
    request = await _pending_request(db, request_id)
    await service.deny_access_request(db, request, admin)


# --- Grants ---------------------------------------------------------------------


@router.get("/grants", response_model=list[GrantOut])
async def list_grants(
    db: DbSession, admin: AdminUser, settings: SettingsDep, include_revoked: bool = False
) -> list[GrantOut]:
    """Grants, newest first, with each user's spend today (USD)."""
    grants = await service.list_grants(db, include_revoked=include_revoked)
    spent = await service.spent_today_by_user(db, [g.user_id for g in grants])
    return [_grant_out(g, settings, spent.get(g.user_id, Decimal(0))) for g in grants]


async def _grant_or_404(db: DbSession, grant_id: uuid.UUID) -> ChatGrant:
    grant = await service.get_grant(db, grant_id)
    if grant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grant not found.")
    return grant


@router.patch("/grants/{grant_id}", response_model=GrantOut)
async def update_grant(
    grant_id: uuid.UUID, body: GrantUpdate, db: DbSession, admin: AdminUser, settings: SettingsDep
) -> GrantOut:
    """Edit a grant's daily limit, expiry or note.

    Only fields present in the body change. Send `"daily_budget_usd": null`
    to go back to the default budget, or `"expires_at": null` to remove expiry.
    """
    grant = await _grant_or_404(db, grant_id)
    try:
        # exclude_unset: only the keys the client actually sent.
        await service.update_grant(db, grant, body.model_dump(exclude_unset=True))
    except service.GrantRevokedError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This grant is revoked. Approve a new one instead."
        ) from None
    return await _grant_out_with_usage(db, grant, settings)


@router.post("/grants/{grant_id}/revoke", response_model=GrantOut)
async def revoke_grant(
    grant_id: uuid.UUID, db: DbSession, admin: AdminUser, settings: SettingsDep
) -> GrantOut:
    """Revoke a grant. Takes effect on the user's next chat request."""
    grant = await _grant_or_404(db, grant_id)
    await service.revoke_grant(db, grant)
    return await _grant_out_with_usage(db, grant, settings)


@router.get("/usage", response_model=UsageReportOut)
async def get_usage(
    db: DbSession,
    admin: AdminUser,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
) -> UsageReportOut:
    """Claude spend for the last `days` UTC days (today included): totals, per
    day (every day, zeros included), per user and per model."""
    return UsageReportOut.from_report(await usage_report(db, days))


# --- Agent runs ------------------------------------------------------------------


@router.get("/agent/runs", response_model=list[AdminRunOut])
async def list_agent_runs(
    db: DbSession, admin: AdminUser, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[AdminRunOut]:
    """Everyone's agent runs, newest first."""
    rows = await db.execute(
        select(AgentRun, User.email)
        .join(User, User.id == AgentRun.user_id)
        .order_by(AgentRun.created_at.desc())
        .limit(limit)
    )
    return [AdminRunOut.from_run(run, email) for run, email in rows]


@router.get("/agent/runs/{run_id}", response_model=RunTrace)
async def get_agent_run_trace(run_id: uuid.UUID, db: DbSession, admin: AdminUser) -> RunTrace:
    """One run with every step as stored: model calls with their tokens and cost,
    tool calls with the exact output the model was given, checks and feedback."""
    row = (
        await db.execute(
            select(AgentRun, User.email)
            .join(User, User.id == AgentRun.user_id)
            .where(AgentRun.id == run_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    run, email = row
    return RunTrace.from_run_steps(run, email, await agent_service.load_steps(db, run))
