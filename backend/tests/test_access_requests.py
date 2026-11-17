"""Request-access form, admin review, and grant management."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccessRequest, ChatGrant, LlmUsage, User
from tests.conftest import FakeEmailSender
from tests.factories import grant, make_user, sign_in_as, signed_in_user

DEFAULT_LIMIT = 50_000


async def submit(client: AsyncClient, email: str, message: str = "") -> None:
    response = await client.post("/access-requests", json={"email": email, "message": message})
    assert response.status_code == 202, response.text
    assert response.json() == {"detail": "Thanks! You'll get an email if your request is approved."}


async def pending_request(db: AsyncSession, email: str) -> AccessRequest:
    request = await db.scalar(select(AccessRequest).where(AccessRequest.email == email))
    assert request is not None
    return request


@pytest.fixture
async def admin(db: AsyncSession, client: AsyncClient) -> User:
    """A signed-in admin; `client` is acting as them."""
    return await signed_in_user(db, client, "owner@example.com", is_admin=True)


# --- Public form -------------------------------------------------------------------


async def test_request_is_stored_and_admins_are_emailed(
    client: AsyncClient, db: AsyncSession, email_outbox: FakeEmailSender
) -> None:
    await make_user(db, "owner@example.com", is_admin=True)
    await make_user(db, "second-admin@example.com", is_admin=True)
    await make_user(db, "not-admin@example.com")

    await submit(client, " Grace@Example.com ", "I'd like to try the trip planner.")

    request = await pending_request(db, "grace@example.com")
    assert request.status == "pending"
    assert request.message == "I'd like to try the trip planner."

    assert sorted(e.to for e in email_outbox.outbox) == [
        "owner@example.com",
        "second-admin@example.com",
    ]
    body = email_outbox.outbox[0].body
    assert "grace@example.com" in body
    assert "I'd like to try the trip planner." in body
    assert "http://localhost:3000/admin" in body


async def test_requests_over_the_daily_limit_are_dropped_silently(
    client: AsyncClient, db: AsyncSession, email_outbox: FakeEmailSender
) -> None:
    await make_user(db, "owner@example.com", is_admin=True)
    for _ in range(4):
        await submit(client, "grace@example.com")  # same response every time

    stored = (await db.scalars(select(AccessRequest))).all()
    assert len(stored) == 3
    assert len(email_outbox.outbox) == 3


async def test_request_without_admins_is_still_stored(
    client: AsyncClient, db: AsyncSession, email_outbox: FakeEmailSender
) -> None:
    await submit(client, "grace@example.com")

    await pending_request(db, "grace@example.com")
    assert email_outbox.outbox == []


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email"},
        {"email": "grace@example.com", "message": "x" * 1001},
    ],
)
async def test_invalid_request_is_422(client: AsyncClient, body: dict[str, str]) -> None:
    assert (await client.post("/access-requests", json=body)).status_code == 422


# --- Admin only ------------------------------------------------------------------------

ADMIN_ROUTES = [
    ("GET", "/admin/access-requests"),
    ("GET", "/admin/grants"),
    ("POST", "/admin/access-requests/00000000-0000-0000-0000-000000000000/approve"),
    ("POST", "/admin/access-requests/00000000-0000-0000-0000-000000000000/deny"),
    ("PATCH", "/admin/grants/00000000-0000-0000-0000-000000000000"),
    ("POST", "/admin/grants/00000000-0000-0000-0000-000000000000/revoke"),
]


@pytest.mark.parametrize(("method", "url"), ADMIN_ROUTES)
async def test_admin_routes_need_sign_in(client: AsyncClient, method: str, url: str) -> None:
    assert (await client.request(method, url, json={})).status_code == 401


@pytest.mark.parametrize(("method", "url"), ADMIN_ROUTES)
async def test_admin_routes_reject_non_admins(
    client: AsyncClient, db: AsyncSession, method: str, url: str
) -> None:
    await signed_in_user(db, client)
    assert (await client.request(method, url, json={})).status_code == 403


# --- Reviewing requests ------------------------------------------------------------------


async def test_list_shows_pending_by_default(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    await submit(client, "grace@example.com", "hello")
    await submit(client, "alan@example.com")
    alan = await pending_request(db, "alan@example.com")
    assert (await client.post(f"/admin/access-requests/{alan.id}/deny")).status_code == 204

    pending = (await client.get("/admin/access-requests")).json()
    assert [r["email"] for r in pending] == ["grace@example.com"]
    assert pending[0]["message"] == "hello"

    denied = (await client.get("/admin/access-requests?status=denied")).json()
    assert [r["email"] for r in denied] == ["alan@example.com"]


async def test_approve_creates_user_and_grant_and_emails_sign_in_link(
    client: AsyncClient, db: AsyncSession, admin: User, email_outbox: FakeEmailSender
) -> None:
    await submit(client, "grace@example.com")
    request = await pending_request(db, "grace@example.com")
    email_outbox.outbox.clear()
    expires = datetime.now(UTC) + timedelta(days=30)

    response = await client.post(
        f"/admin/access-requests/{request.id}/approve",
        json={"note": "Recruiter", "daily_token_limit": 20_000, "expires_at": expires.isoformat()},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_email"] == "grace@example.com"
    assert body["note"] == "Recruiter"
    assert body["daily_token_limit"] == 20_000
    assert body["effective_daily_token_limit"] == 20_000
    assert datetime.fromisoformat(body["expires_at"]) == expires

    await db.refresh(request)
    assert request.status == "approved"
    assert request.decided_by_id == admin.id
    assert request.decided_at is not None
    stored = await db.scalar(select(ChatGrant).where(ChatGrant.id == body["id"]))
    assert stored is not None and stored.granted_by_id == admin.id

    # The requester gets one email with a working sign-in link, plus the
    # /login fallback for when that link has expired.
    [sent] = email_outbox.outbox
    assert sent.to == "grace@example.com"
    assert "http://localhost:3000/login" in sent.body
    match = re.search(r"/auth/verify\?token=([\w-]+)", sent.body)
    assert match, sent.body

    client.cookies.clear()
    assert (await client.post("/auth/verify", json={"token": match.group(1)})).status_code == 200
    access = (await client.get("/chat/access")).json()
    assert access["allowed"] is True
    assert access["daily_token_limit"] == 20_000


async def test_approve_with_defaults_follows_default_limit(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    await submit(client, "grace@example.com")
    request = await pending_request(db, "grace@example.com")

    body = (await client.post(f"/admin/access-requests/{request.id}/approve", json={})).json()

    assert body["daily_token_limit"] is None
    assert body["effective_daily_token_limit"] == DEFAULT_LIMIT
    assert body["expires_at"] is None


async def test_approve_existing_user_reuses_account(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    existing = await make_user(db, "grace@example.com")
    await submit(client, "grace@example.com")
    request = await pending_request(db, "grace@example.com")

    body = (await client.post(f"/admin/access-requests/{request.id}/approve", json={})).json()

    assert body["user_id"] == str(existing.id)


async def test_approve_user_with_active_grant_is_409_and_changes_nothing(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    grace = await make_user(db, "grace@example.com")
    await grant(db, grace)
    await submit(client, "grace@example.com")
    request = await pending_request(db, "grace@example.com")

    response = await client.post(f"/admin/access-requests/{request.id}/approve", json={})

    assert response.status_code == 409
    assert "already has an active grant" in response.json()["detail"]
    pending = (await client.get("/admin/access-requests")).json()
    assert [r["email"] for r in pending] == ["grace@example.com"]


async def test_approve_user_with_expired_grant_replaces_it(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    grace = await make_user(db, "grace@example.com")
    old = await grant(db, grace, expires_at=datetime.now(UTC) - timedelta(days=1))
    await submit(client, "grace@example.com")
    request = await pending_request(db, "grace@example.com")

    response = await client.post(f"/admin/access-requests/{request.id}/approve", json={})

    assert response.status_code == 200
    await db.refresh(old)
    assert old.revoked_at is not None


@pytest.mark.parametrize("first", ["approve", "deny"])
@pytest.mark.parametrize("second", ["approve", "deny"])
async def test_deciding_twice_is_409(
    client: AsyncClient, db: AsyncSession, admin: User, first: str, second: str
) -> None:
    await submit(client, "grace@example.com")
    request = await pending_request(db, "grace@example.com")
    url = f"/admin/access-requests/{request.id}"

    assert (await client.post(f"{url}/{first}", json={})).status_code in (200, 204)
    response = await client.post(f"{url}/{second}", json={})

    assert response.status_code == 409
    past_tense = {"approve": "approved", "deny": "denied"}[first]
    assert f"already {past_tense}" in response.json()["detail"]


async def test_deny_does_not_email_requester(
    client: AsyncClient, db: AsyncSession, admin: User, email_outbox: FakeEmailSender
) -> None:
    await submit(client, "grace@example.com")
    request = await pending_request(db, "grace@example.com")
    email_outbox.outbox.clear()

    assert (await client.post(f"/admin/access-requests/{request.id}/deny")).status_code == 204

    await db.refresh(request)
    assert request.status == "denied"
    assert request.decided_by_id == admin.id
    assert email_outbox.outbox == []
    assert await db.scalar(select(User).where(User.email == "grace@example.com")) is None


async def test_unknown_request_is_404(client: AsyncClient, admin: User) -> None:
    url = "/admin/access-requests/00000000-0000-0000-0000-000000000000"
    assert (await client.post(f"{url}/approve", json={})).status_code == 404
    assert (await client.post(f"{url}/deny")).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"expires_at": "2030-01-01T00:00:00"},  # no timezone
        {"daily_token_limit": 0},
        {"note": "x" * 501},
    ],
)
async def test_invalid_approval_is_422(
    client: AsyncClient, db: AsyncSession, admin: User, body: dict[str, object]
) -> None:
    await submit(client, "grace@example.com")
    request = await pending_request(db, "grace@example.com")
    response = await client.post(f"/admin/access-requests/{request.id}/approve", json=body)
    assert response.status_code == 422


# --- Managing grants ---------------------------------------------------------------------


async def test_list_grants_with_usage(client: AsyncClient, db: AsyncSession, admin: User) -> None:
    grace = await make_user(db, "grace@example.com")
    await grant(db, grace, daily_token_limit=1_000)
    alan = await make_user(db, "alan@example.com")
    await grant(db, alan, revoked_at=datetime.now(UTC))
    db.add(LlmUsage(user_id=grace.id, model="m", input_tokens=100, output_tokens=50, cost_usd=0))
    await db.commit()

    grants = (await client.get("/admin/grants")).json()
    assert [g["user_email"] for g in grants] == ["grace@example.com"]
    assert grants[0]["tokens_used_today"] == 150
    assert grants[0]["effective_daily_token_limit"] == 1_000

    everything = (await client.get("/admin/grants?include_revoked=true")).json()
    assert {g["user_email"] for g in everything} == {"grace@example.com", "alan@example.com"}


async def test_edit_grant_limit_takes_effect(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    grace = await make_user(db, "grace@example.com")
    g = await grant(db, grace, note="Recruiter")

    response = await client.patch(f"/admin/grants/{g.id}", json={"daily_token_limit": 2_000})
    assert response.status_code == 200
    assert response.json()["daily_token_limit"] == 2_000
    assert response.json()["note"] == "Recruiter"  # fields not sent are unchanged

    await sign_in_as(db, client, grace)
    assert (await client.get("/chat/access")).json()["daily_token_limit"] == 2_000


async def test_edit_grant_null_limit_returns_to_default(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    grace = await make_user(db, "grace@example.com")
    g = await grant(db, grace, daily_token_limit=2_000)

    body = (await client.patch(f"/admin/grants/{g.id}", json={"daily_token_limit": None})).json()

    assert body["daily_token_limit"] is None
    assert body["effective_daily_token_limit"] == DEFAULT_LIMIT


async def test_edit_grant_expiry_and_note(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    grace = await make_user(db, "grace@example.com")
    g = await grant(db, grace, daily_token_limit=2_000)
    expires = datetime.now(UTC) + timedelta(days=3)

    body = (
        await client.patch(
            f"/admin/grants/{g.id}", json={"expires_at": expires.isoformat(), "note": "Extended"}
        )
    ).json()

    assert datetime.fromisoformat(body["expires_at"]) == expires
    assert body["note"] == "Extended"
    assert body["daily_token_limit"] == 2_000


async def test_edit_revoked_grant_is_409(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    grace = await make_user(db, "grace@example.com")
    g = await grant(db, grace, revoked_at=datetime.now(UTC))

    response = await client.patch(f"/admin/grants/{g.id}", json={"daily_token_limit": 5})
    assert response.status_code == 409


async def test_revoke_grant_blocks_chat_and_is_idempotent(
    client: AsyncClient, db: AsyncSession, admin: User
) -> None:
    grace = await make_user(db, "grace@example.com")
    g = await grant(db, grace)

    first = await client.post(f"/admin/grants/{g.id}/revoke")
    assert first.status_code == 200
    revoked_at = first.json()["revoked_at"]
    assert revoked_at is not None

    second = await client.post(f"/admin/grants/{g.id}/revoke")
    assert second.json()["revoked_at"] == revoked_at

    await sign_in_as(db, client, grace)
    assert (await client.get("/chat/access")).json()["reason"] == "no_grant"


async def test_unknown_grant_is_404(client: AsyncClient, admin: User) -> None:
    url = "/admin/grants/00000000-0000-0000-0000-000000000000"
    assert (await client.patch(url, json={})).status_code == 404
    assert (await client.post(f"{url}/revoke")).status_code == 404
