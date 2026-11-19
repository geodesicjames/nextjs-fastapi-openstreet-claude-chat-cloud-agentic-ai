"""Magic-link sign-in, sessions and logout."""

import re
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import LoginToken, User, UserSession
from app.security import hash_token
from app.services.auth import delete_expired
from tests.conftest import FakeEmailSender
from tests.factories import make_user

COOKIE = get_settings().session_cookie_name


async def request_link(client: AsyncClient, outbox: FakeEmailSender, email: str) -> str:
    """Request a sign-in link and return the token from the emailed URL."""
    response = await client.post("/auth/login-link", json={"email": email})
    assert response.status_code == 202
    match = re.search(r"/auth/verify\?token=([\w-]+)", outbox.outbox[-1].body)
    assert match, outbox.outbox[-1].body
    return match.group(1)


async def sign_in(client: AsyncClient, outbox: FakeEmailSender, email: str) -> None:
    token = await request_link(client, outbox, email)
    response = await client.post("/auth/verify", json={"token": token})
    assert response.status_code == 200, response.text


async def test_full_sign_in_flow(
    client: AsyncClient, email_outbox: FakeEmailSender, db: AsyncSession
) -> None:
    token = await request_link(client, email_outbox, "Ada@Example.com")
    sent = email_outbox.outbox[-1]
    assert sent.to == "ada@example.com"
    assert sent.body.count("http://localhost:3000/auth/verify?token=") == 1

    response = await client.post("/auth/verify", json={"token": token})
    assert response.status_code == 200
    assert response.json()["email"] == "ada@example.com"

    set_cookie = response.headers["set-cookie"].lower()
    assert f"{COOKIE}=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert f"max-age={30 * 24 * 60 * 60}" in set_cookie

    me = await client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "ada@example.com"

    # Only hashes are stored, never the raw tokens.
    stored = await db.scalar(select(LoginToken.token_hash))
    assert stored is not None and stored != token
    session = await db.scalar(select(UserSession))
    assert session is not None
    assert session.token_hash == hash_token(client.cookies[COOKIE])
    assert session.token_hash != client.cookies[COOKIE]


async def test_first_sign_in_creates_user_once(
    client: AsyncClient, email_outbox: FakeEmailSender, db: AsyncSession
) -> None:
    await sign_in(client, email_outbox, "grace@example.com")
    await sign_in(client, email_outbox, "GRACE@example.com")

    users = (await db.scalars(select(User).where(User.email == "grace@example.com"))).all()
    assert len(users) == 1
    assert users[0].last_login_at is not None


async def test_link_cannot_be_used_twice(
    client: AsyncClient, email_outbox: FakeEmailSender
) -> None:
    token = await request_link(client, email_outbox, "a@example.com")
    assert (await client.post("/auth/verify", json={"token": token})).status_code == 200

    second = await client.post("/auth/verify", json={"token": token})
    assert second.status_code == 400


async def test_expired_link_is_rejected(
    client: AsyncClient, email_outbox: FakeEmailSender, db: AsyncSession
) -> None:
    token = await request_link(client, email_outbox, "a@example.com")
    await db.execute(update(LoginToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))

    response = await client.post("/auth/verify", json={"token": token})
    assert response.status_code == 400
    assert COOKIE not in response.cookies


async def test_link_expires_after_30_minutes(
    client: AsyncClient, email_outbox: FakeEmailSender, db: AsyncSession
) -> None:
    await request_link(client, email_outbox, "a@example.com")
    login_token = await db.scalar(select(LoginToken))
    assert login_token is not None

    lifetime = login_token.expires_at - datetime.now(UTC)
    assert timedelta(minutes=29) < lifetime <= timedelta(minutes=30)


async def test_unknown_token_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/auth/verify", json={"token": "not-a-real-token"})
    assert response.status_code == 400


async def test_login_link_response_does_not_reveal_accounts(
    client: AsyncClient, email_outbox: FakeEmailSender
) -> None:
    await sign_in(client, email_outbox, "existing@example.com")

    existing = await client.post("/auth/login-link", json={"email": "existing@example.com"})
    new = await client.post("/auth/login-link", json={"email": "nobody@example.com"})

    assert existing.status_code == new.status_code == 202
    assert existing.json() == new.json()


async def test_login_links_are_rate_limited_per_email(
    client: AsyncClient, email_outbox: FakeEmailSender
) -> None:
    limit = get_settings().login_link_max_per_hour
    for _ in range(limit + 2):
        response = await client.post("/auth/login-link", json={"email": "spam@example.com"})
        assert response.status_code == 202

    assert len(email_outbox.outbox) == limit

    # Other addresses are unaffected.
    await client.post("/auth/login-link", json={"email": "other@example.com"})
    assert len(email_outbox.outbox) == limit + 1


async def test_invalid_email_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/auth/login-link", json={"email": "not-an-email"})
    assert response.status_code == 422


async def test_me_requires_sign_in(client: AsyncClient) -> None:
    assert (await client.get("/auth/me")).status_code == 401


async def test_unknown_session_cookie_is_ignored(client: AsyncClient) -> None:
    client.cookies.set(COOKIE, "forged")
    assert (await client.get("/auth/me")).status_code == 401


async def test_expired_session_is_rejected(
    client: AsyncClient, email_outbox: FakeEmailSender, db: AsyncSession
) -> None:
    await sign_in(client, email_outbox, "a@example.com")
    await db.execute(
        update(UserSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    assert (await client.get("/auth/me")).status_code == 401


async def test_logout_ends_the_session(
    client: AsyncClient, email_outbox: FakeEmailSender, db: AsyncSession
) -> None:
    await sign_in(client, email_outbox, "a@example.com")
    cookie_value = client.cookies[COOKIE]

    response = await client.post("/auth/logout")
    assert response.status_code == 204
    assert f'{COOKIE}=""' in response.headers["set-cookie"]

    # The session row is gone, so the old cookie is useless even if replayed.
    assert await db.scalar(select(UserSession)) is None
    client.cookies.set(COOKIE, cookie_value)
    assert (await client.get("/auth/me")).status_code == 401


async def test_logout_without_session_is_harmless(client: AsyncClient) -> None:
    assert (await client.post("/auth/logout")).status_code == 204


async def test_cleanup_deletes_only_stale_tokens_and_expired_sessions(db: AsyncSession) -> None:
    now = datetime.now(UTC)
    user = await make_user(db, "a@example.com")
    db.add_all(
        [
            # Old enough to go (also outside the rate-limit window).
            LoginToken(
                token_hash="t-old",
                email="a@example.com",
                expires_at=now - timedelta(days=2),
                created_at=now - timedelta(days=2),
            ),
            # Expired, but recent: still counts towards the per-address limit.
            LoginToken(
                token_hash="t-recent",
                email="a@example.com",
                expires_at=now - timedelta(minutes=5),
                created_at=now - timedelta(minutes=35),
            ),
            UserSession(
                token_hash="s-expired",
                user_id=user.id,
                expires_at=now - timedelta(seconds=1),
                last_seen_at=now - timedelta(days=30),
            ),
            UserSession(
                token_hash="s-live",
                user_id=user.id,
                expires_at=now + timedelta(days=1),
                last_seen_at=now,
            ),
        ]
    )
    await db.commit()

    assert await delete_expired(db) == (1, 1)
    assert set(await db.scalars(select(LoginToken.token_hash))) == {"t-recent"}
    assert set(await db.scalars(select(UserSession.token_hash))) == {"s-live"}
