"""Shared behaviour for clients of external services (Overpass, Nominatim).

- `UpstreamClient`: process-wide state for one service, kept on `app.state`.
  It holds a lock (one request at a time) and an exponential cooldown
  (30 s doubling to 10 min, reset by a success) after the service says
  "slow down" or fails to answer.
- `log_call` / `usage_today`: every request is logged in `upstream_calls`,
  which is how the daily budgets are enforced across processes and restarts.
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UpstreamCall
from app.services.chat_access import start_of_day

COOLDOWN_SECONDS = 30
MAX_COOLDOWN_SECONDS = 600
# "Too busy / slow down": 406 and 429 per the OSM services' usage policies;
# 502/503/504 are what overloaded servers return (Overpass sends 502 "at
# capacity" and asks clients to back off exponentially).
BACKOFF_STATUSES = {406, 429, 502, 503, 504}


@dataclass
class UpstreamClient:
    http: httpx.AsyncClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cooldown_until: float = 0.0  # time.monotonic() value
    consecutive_failures: int = 0

    def in_cooldown(self) -> bool:
        return time.monotonic() < self.cooldown_until

    def back_off(self) -> float:
        """Start a cooldown that doubles with each consecutive failure."""
        seconds = min(COOLDOWN_SECONDS * 2**self.consecutive_failures, MAX_COOLDOWN_SECONDS)
        self.consecutive_failures += 1
        self.cooldown_until = time.monotonic() + seconds
        return seconds

    def succeeded(self) -> None:
        self.consecutive_failures = 0


async def log_call(
    db: AsyncSession, service: str, status_code: int | None, response_bytes: int, started: float
) -> None:
    """Record one request (`started` is a time.monotonic() value)."""
    db.add(
        UpstreamCall(
            service=service,
            status_code=status_code,
            response_bytes=response_bytes,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    )
    await db.commit()


async def usage_today(db: AsyncSession, service: str) -> tuple[int, int]:
    """(calls, response bytes) for `service` since midnight UTC."""
    calls, total_bytes = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(UpstreamCall.response_bytes), 0)).where(
                UpstreamCall.service == service,
                UpstreamCall.created_at >= start_of_day(datetime.now(UTC)),
            )
        )
    ).one()
    return int(calls), int(total_bytes)
