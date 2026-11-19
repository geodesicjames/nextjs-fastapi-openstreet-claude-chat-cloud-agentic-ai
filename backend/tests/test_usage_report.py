"""GET /admin/usage: Claude spend by day, user and model."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LlmUsage, User
from tests.factories import make_user, signed_in_user


def at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=UTC)


def usage(user: User, when: datetime, cost: str, model: str = "claude-haiku-5-5") -> LlmUsage:
    row = LlmUsage(
        user_id=user.id,
        model=model,
        input_tokens=100,
        cache_creation_input_tokens=20,
        cache_read_input_tokens=30,
        output_tokens=40,
        cost_usd=Decimal(cost),
    )
    row.created_at = when
    return row


async def test_report_sums_by_day_user_and_model(db: AsyncSession, client: AsyncClient) -> None:
    await signed_in_user(db, client, "owner@example.com", is_admin=True)
    ada = await make_user(db, "ada@example.com")
    bob = await make_user(db, "bob@example.com")
    today = datetime.now(UTC).date()
    db.add_all(
        [
            usage(ada, at(today), "0.10"),
            usage(ada, at(today, 0), "0.05", model="claude-sonnet-5-5"),
            usage(bob, at(today - timedelta(days=2)), "0.30"),
            # Outside a 7-day period: day 7 back is excluded (today is day 1).
            usage(bob, at(today - timedelta(days=7)), "9.00"),
        ]
    )
    await db.commit()

    response = await client.get("/admin/usage", params={"days": 7})
    assert response.status_code == 200
    report = response.json()

    assert report["start"] == (today - timedelta(days=6)).isoformat()
    assert report["end"] == today.isoformat()
    assert report["total"] == {
        "cost_usd": 0.45,
        "calls": 3,
        "input_tokens": 3 * 150,  # uncached + cache writes + cache reads
        "output_tokens": 3 * 40,
    }

    # Every day appears, oldest first, zeros included.
    days = {d["day"]: d for d in report["by_day"]}
    assert len(report["by_day"]) == 7
    assert report["by_day"][-1]["day"] == today.isoformat()
    assert days[today.isoformat()]["cost_usd"] == 0.15
    assert days[today.isoformat()]["calls"] == 2
    assert days[(today - timedelta(days=2)).isoformat()]["cost_usd"] == 0.3
    assert days[(today - timedelta(days=1)).isoformat()] == {
        "day": (today - timedelta(days=1)).isoformat(),
        "cost_usd": 0.0,
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }

    # Biggest spender first.
    assert [(u["email"], u["cost_usd"]) for u in report["by_user"]] == [
        ("bob@example.com", 0.3),
        ("ada@example.com", 0.15),
    ]
    assert [(m["model"], m["calls"]) for m in report["by_model"]] == [
        ("claude-haiku-5-5", 2),
        ("claude-sonnet-5-5", 1),
    ]


async def test_empty_period_is_all_zeros(db: AsyncSession, client: AsyncClient) -> None:
    await signed_in_user(db, client, "owner@example.com", is_admin=True)
    report = (await client.get("/admin/usage")).json()
    assert len(report["by_day"]) == 30  # the default period
    assert report["total"]["cost_usd"] == 0
    assert report["by_user"] == [] and report["by_model"] == []


async def test_days_must_be_in_range(db: AsyncSession, client: AsyncClient) -> None:
    await signed_in_user(db, client, "owner@example.com", is_admin=True)
    assert (await client.get("/admin/usage", params={"days": 0})).status_code == 422
    assert (await client.get("/admin/usage", params={"days": 367})).status_code == 422


async def test_admins_only(db: AsyncSession, client: AsyncClient) -> None:
    assert (await client.get("/admin/usage")).status_code == 401
    await signed_in_user(db, client, "ada@example.com")
    assert (await client.get("/admin/usage")).status_code == 403
