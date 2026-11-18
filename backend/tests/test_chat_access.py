"""Invite-only chat access: grants, daily token budgets and chat_mode.

`require_chat_access` guards routes that don't exist yet, so these tests add a
small route that uses it (`/test/guarded`) to exercise it over HTTP, the same
way a real chat route will.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import ChatAccess
from app.models import ChatGrant, LlmUsage, User
from app.services.chat_access import make_admin, record_usage, start_of_day
from tests.factories import grant, signed_in_user

GUARDED = "/test/guarded"
DEFAULT_LIMIT = 50_000


@pytest.fixture(autouse=True)
def guarded_route(app: FastAPI) -> None:
    @app.get(GUARDED)
    async def guarded(access: ChatAccess) -> dict[str, int]:
        return {"remaining": access.remaining}


async def use_tokens(db: AsyncSession, user: User, tokens: int, at: datetime | None = None) -> None:
    usage = LlmUsage(
        user_id=user.id,
        model="test-model",
        input_tokens=tokens // 2,
        output_tokens=tokens - tokens // 2,
        cost_usd=Decimal("0.01"),
    )
    if at is not None:
        usage.created_at = at
    db.add(usage)
    await db.commit()


# --- Who gets in -----------------------------------------------------------


async def test_not_signed_in_is_401(client: AsyncClient) -> None:
    assert (await client.get(GUARDED)).status_code == 401
    assert (await client.get("/chat/access")).status_code == 401


async def test_no_grant_is_403(client: AsyncClient, db: AsyncSession) -> None:
    await signed_in_user(db, client)

    response = await client.get(GUARDED)
    assert response.status_code == 403

    access = (await client.get("/chat/access")).json()
    assert access["allowed"] is False
    assert access["reason"] == "no_grant"
    assert access["mode"] == "invite_only"


async def test_active_grant_allows_chat(client: AsyncClient, db: AsyncSession) -> None:
    user = await signed_in_user(db, client)
    await grant(db, user)

    response = await client.get(GUARDED)
    assert response.status_code == 200
    assert response.json() == {"remaining": DEFAULT_LIMIT}

    access = (await client.get("/chat/access")).json()
    assert access["allowed"] is True
    assert access["reason"] is None
    assert access["daily_token_limit"] == DEFAULT_LIMIT
    assert access["tokens_used_today"] == 0


async def test_expired_grant_is_403(client: AsyncClient, db: AsyncSession) -> None:
    user = await signed_in_user(db, client)
    await grant(db, user, expires_at=datetime.now(UTC) - timedelta(minutes=1))

    assert (await client.get(GUARDED)).status_code == 403


async def test_grant_with_future_expiry_allows_chat(client: AsyncClient, db: AsyncSession) -> None:
    user = await signed_in_user(db, client)
    expires = datetime.now(UTC) + timedelta(days=7)
    await grant(db, user, expires_at=expires)

    assert (await client.get(GUARDED)).status_code == 200
    access = (await client.get("/chat/access")).json()
    assert datetime.fromisoformat(access["grant_expires_at"]) == expires


async def test_revoked_grant_is_403(client: AsyncClient, db: AsyncSession) -> None:
    user = await signed_in_user(db, client)
    await grant(db, user, revoked_at=datetime.now(UTC))

    assert (await client.get(GUARDED)).status_code == 403


async def test_another_users_grant_does_not_count(client: AsyncClient, db: AsyncSession) -> None:
    other = User(email="other@example.com")
    db.add(other)
    await db.flush()
    await grant(db, other)
    await signed_in_user(db, client)

    assert (await client.get(GUARDED)).status_code == 403


# --- Daily budget ------------------------------------------------------------


async def test_usage_today_counts_against_budget(client: AsyncClient, db: AsyncSession) -> None:
    user = await signed_in_user(db, client)
    await grant(db, user)
    await use_tokens(db, user, 1_234)

    assert (await client.get(GUARDED)).json() == {"remaining": DEFAULT_LIMIT - 1_234}
    assert (await client.get("/chat/access")).json()["tokens_used_today"] == 1_234


async def test_budget_used_up_is_429_with_retry_after(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = await signed_in_user(db, client)
    await grant(db, user)
    await use_tokens(db, user, DEFAULT_LIMIT)

    response = await client.get(GUARDED)
    assert response.status_code == 429
    retry_after = int(response.headers["retry-after"])
    assert 0 < retry_after <= 24 * 60 * 60

    access = (await client.get("/chat/access")).json()
    assert access["allowed"] is False
    assert access["reason"] == "budget_exhausted"
    assert access["tokens_remaining"] == 0


async def test_usage_before_midnight_utc_does_not_count(
    client: AsyncClient, db: AsyncSession
) -> None:
    user = await signed_in_user(db, client)
    await grant(db, user)
    yesterday = start_of_day(datetime.now(UTC)) - timedelta(seconds=1)
    await use_tokens(db, user, DEFAULT_LIMIT, at=yesterday)

    assert (await client.get(GUARDED)).status_code == 200


async def test_another_users_usage_does_not_count(client: AsyncClient, db: AsyncSession) -> None:
    other = User(email="other@example.com")
    db.add(other)
    await db.flush()
    await use_tokens(db, other, DEFAULT_LIMIT)
    user = await signed_in_user(db, client)
    await grant(db, user)

    assert (await client.get(GUARDED)).json() == {"remaining": DEFAULT_LIMIT}


async def test_grant_limit_overrides_default(client: AsyncClient, db: AsyncSession) -> None:
    user = await signed_in_user(db, client)
    await grant(db, user, daily_token_limit=1_000)
    await use_tokens(db, user, 1_000)

    assert (await client.get(GUARDED)).status_code == 429
    assert (await client.get("/chat/access")).json()["daily_token_limit"] == 1_000


async def test_default_limit_setting_applies_to_grants_without_their_own(
    client: AsyncClient, db: AsyncSession, use_settings: Callable[..., None]
) -> None:
    use_settings(chat_default_daily_tokens=500)
    user = await signed_in_user(db, client)
    await grant(db, user)

    assert (await client.get(GUARDED)).json() == {"remaining": 500}


async def test_record_usage_is_counted(client: AsyncClient, db: AsyncSession) -> None:
    user = await signed_in_user(db, client)
    g = await grant(db, user)
    usage = await record_usage(
        db,
        user=user,
        grant=g,
        model="claude-test",
        input_tokens=300,
        output_tokens=200,
        cost_usd=Decimal("0.004500"),
    )

    assert usage.grant_id == g.id
    assert (await client.get(GUARDED)).json() == {"remaining": DEFAULT_LIMIT - 500}


# --- Open mode ---------------------------------------------------------------


async def test_open_mode_allows_signed_in_users_without_grant(
    client: AsyncClient, db: AsyncSession, use_settings: Callable[..., None]
) -> None:
    use_settings(chat_mode="open")
    await signed_in_user(db, client)

    assert (await client.get(GUARDED)).json() == {"remaining": DEFAULT_LIMIT}
    assert (await client.get("/chat/access")).json()["mode"] == "open"


async def test_open_mode_still_needs_sign_in(
    client: AsyncClient, use_settings: Callable[..., None]
) -> None:
    use_settings(chat_mode="open")
    assert (await client.get(GUARDED)).status_code == 401


async def test_open_mode_still_enforces_budget(
    client: AsyncClient, db: AsyncSession, use_settings: Callable[..., None]
) -> None:
    use_settings(chat_mode="open")
    user = await signed_in_user(db, client)
    await use_tokens(db, user, DEFAULT_LIMIT)

    assert (await client.get(GUARDED)).status_code == 429


# --- Database rules ---------------------------------------------------------------


async def test_only_one_active_grant_per_user(db: AsyncSession) -> None:
    user = User(email="ada@example.com")
    db.add(user)
    await db.flush()
    await grant(db, user)

    # begin_nested() is a savepoint, so the failed insert can be rolled back
    # without losing the rest of the test's data.
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(ChatGrant(user_id=user.id))

    # A revoked grant doesn't count, so revoke-then-regrant works.
    old = await db.scalar(select(ChatGrant).where(ChatGrant.user_id == user.id))
    assert old is not None
    old.revoked_at = datetime.now(UTC)
    await grant(db, user)


# --- make-admin ----------------------------------------------------------------


async def test_make_admin_creates_user_and_grant(db: AsyncSession) -> None:
    user, g = await make_admin(db, " Owner@Example.com ")

    assert user.email == "owner@example.com"
    assert user.is_admin is True
    assert g.user_id == user.id
    assert g.revoked_at is None


async def test_make_admin_is_idempotent(db: AsyncSession) -> None:
    user, first = await make_admin(db, "owner@example.com")
    again, second = await make_admin(db, "owner@example.com")

    assert again.id == user.id
    assert second.id == first.id


async def test_make_admin_replaces_expired_grant(db: AsyncSession) -> None:
    user = User(email="owner@example.com")
    db.add(user)
    await db.flush()
    expired = await grant(db, user, expires_at=datetime.now(UTC) - timedelta(days=1))

    _, new = await make_admin(db, "owner@example.com")

    assert new.id != expired.id
    assert expired.revoked_at is not None
