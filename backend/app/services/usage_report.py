"""Claude spend over a period, from the `llm_usage` ledger, for the admin page.

Days are UTC calendar days, the same days the per-user budgets reset on.
Every day in the period appears in `by_day`, with zeros where nothing was
spent, so a chart of it has no gaps.

The aggregation happens in Postgres (GROUP BY), not in Python: the ledger
gains a row per model call, and only the sums are needed.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LlmUsage, User


@dataclass
class Totals:
    cost_usd: Decimal = Decimal(0)
    calls: int = 0
    input_tokens: int = 0  # uncached input + cache writes + cache reads
    output_tokens: int = 0


@dataclass
class DayUsage(Totals):
    day: date = date.min


@dataclass
class UserUsage(Totals):
    email: str = ""


@dataclass
class ModelUsage(Totals):
    model: str = ""


@dataclass
class UsageReport:
    start: date
    end: date  # inclusive: today
    total: Totals
    by_day: list[DayUsage]
    by_user: list[UserUsage]
    by_model: list[ModelUsage]


def _sums():
    """The aggregate columns every breakdown shares."""
    return (
        func.coalesce(func.sum(LlmUsage.cost_usd), 0),
        func.count(LlmUsage.id),
        func.coalesce(
            func.sum(
                LlmUsage.input_tokens
                + LlmUsage.cache_creation_input_tokens
                + LlmUsage.cache_read_input_tokens
            ),
            0,
        ),
        func.coalesce(func.sum(LlmUsage.output_tokens), 0),
    )


def _totals(row) -> dict:
    cost, calls, input_tokens, output_tokens = row
    return {
        "cost_usd": Decimal(cost),
        "calls": int(calls),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
    }


async def usage_report(db: AsyncSession, days: int, *, today: date | None = None) -> UsageReport:
    """Spend for the last `days` UTC days, today included."""
    end = today or datetime.now(UTC).date()
    start = end - timedelta(days=days - 1)
    since = datetime.combine(start, time.min, tzinfo=UTC)
    in_period = LlmUsage.created_at >= since

    # created_at is timestamptz; convert to UTC before taking the date, or
    # Postgres would use the session's time zone.
    day_col = cast(func.timezone("UTC", LlmUsage.created_at), Date).label("day")
    day_rows = await db.execute(
        select(day_col, *_sums()).where(in_period).group_by(day_col).order_by(day_col)
    )
    spent = {row[0]: DayUsage(day=row[0], **_totals(row[1:])) for row in day_rows}
    by_day = [
        spent.get(start + timedelta(days=i), DayUsage(day=start + timedelta(days=i)))
        for i in range(days)
    ]

    user_rows = await db.execute(
        select(User.email, *_sums())
        .join(User, User.id == LlmUsage.user_id)
        .where(in_period)
        .group_by(User.email)
        .order_by(func.sum(LlmUsage.cost_usd).desc(), User.email)
    )
    by_user = [UserUsage(email=row[0], **_totals(row[1:])) for row in user_rows]

    model_rows = await db.execute(
        select(LlmUsage.model, *_sums())
        .where(in_period)
        .group_by(LlmUsage.model)
        .order_by(func.sum(LlmUsage.cost_usd).desc())
    )
    by_model = [ModelUsage(model=row[0], **_totals(row[1:])) for row in model_rows]

    total = Totals(**_totals((await db.execute(select(*_sums()).where(in_period))).one()))
    return UsageReport(
        start=start, end=end, total=total, by_day=by_day, by_user=by_user, by_model=by_model
    )
