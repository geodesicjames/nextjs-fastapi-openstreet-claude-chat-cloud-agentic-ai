"""Fetching places from the Overpass API, within its usage policy.

The public instance (https://wiki.openstreetmap.org/wiki/Overpass_API) asks
apps that use it regularly to stay under roughly 100 queries and 10 MB a day,
summed over all users; to identify themselves with a User-Agent; never to run
queries in parallel; and to pause for 30 seconds after a 429 or 406.

This module enforces all of that:
- a daily budget counted from the `upstream_calls` table (shared by every
  worker process, and it survives restarts);
- one query at a time per process (`OverpassClient.lock`);
- an exponential cooldown after "slow down" responses or failures to answer
  (30 s, doubling to at most 10 minutes, reset by a successful call);
- the User-Agent from settings.

It only talks to Overpass. Caching decisions live in app/services/places.py.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import UpstreamCall
from app.services.categories import CATEGORIES, category_of
from app.services.chat_access import start_of_day
from app.services.grid import BBox

logger = logging.getLogger(__name__)

SERVICE = "overpass"
COOLDOWN_SECONDS = 30
MAX_COOLDOWN_SECONDS = 600
# Statuses that mean "too busy / slow down": 406 and 429 per the usage policy;
# 502/503/504 are what the server returns when its queue is full ("at
# capacity", asking clients to back off exponentially).
BACKOFF_STATUSES = {406, 429, 502, 503, 504}


# Why a fetch didn't happen or failed. "busy" is added by the places service.
UnavailableReason = Literal["budget", "cooldown", "upstream_error"]


class OverpassUnavailableError(Exception):
    """No query was made or it failed. `reason` says why, for the response."""

    def __init__(self, reason: UnavailableReason) -> None:
        super().__init__(reason)
        self.reason: UnavailableReason = reason


@dataclass
class OverpassElement:
    osm_type: str
    osm_id: int
    lat: float
    lon: float
    category: str
    name: str | None
    tags: dict[str, Any]


@dataclass
class OverpassClient:
    """Process-wide Overpass state. One instance lives on `app.state`."""

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

    async def fetch(
        self, db: AsyncSession, settings: Settings, bbox: BBox
    ) -> list[OverpassElement]:
        """Fetch all categorised places in `bbox`. The caller holds `lock`.

        Raises OverpassUnavailableError (reason "budget", "cooldown" or
        "upstream_error") instead of making or completing the call.
        """
        if self.in_cooldown():
            raise OverpassUnavailableError("cooldown")
        if not await within_budget(db, settings):
            raise OverpassUnavailableError("budget")

        query = build_query(bbox, settings.overpass_timeout_seconds)
        started = time.monotonic()
        status: int | None = None
        size = 0
        try:
            response = await self.http.post(
                settings.overpass_url,
                data={"data": query},
                headers={"User-Agent": settings.user_agent},
                # A little longer than the server-side [timeout:] in the query.
                timeout=settings.overpass_timeout_seconds + 5,
            )
            status, size = response.status_code, len(response.content)
        except httpx.HTTPError as error:
            # Timeouts and connection failures usually mean the server is
            # overloaded or unreachable, so back off just as for a 429.
            seconds = self.back_off()
            logger.warning("Overpass request failed: %r; pausing %ss", error, seconds)
            raise OverpassUnavailableError("upstream_error") from error
        finally:
            db.add(
                UpstreamCall(
                    service=SERVICE,
                    status_code=status,
                    response_bytes=size,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            )
            await db.commit()

        if status in BACKOFF_STATUSES:
            seconds = self.back_off()
            logger.warning("Overpass returned %s; pausing %ss", status, seconds)
            raise OverpassUnavailableError("upstream_error")
        if status != 200:
            logger.warning("Overpass returned %s", status)
            raise OverpassUnavailableError("upstream_error")

        self.consecutive_failures = 0
        try:
            return parse_response(response.json())
        except (ValueError, KeyError, TypeError) as error:
            logger.warning("Unexpected Overpass response: %r", error)
            raise OverpassUnavailableError("upstream_error") from error


async def within_budget(db: AsyncSession, settings: Settings) -> bool:
    """True if today's (UTC) Overpass calls and bytes are under budget."""
    calls, total_bytes = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(UpstreamCall.response_bytes), 0)).where(
                UpstreamCall.service == SERVICE,
                UpstreamCall.created_at >= start_of_day(datetime.now(UTC)),
            )
        )
    ).one()
    return (
        calls < settings.overpass_daily_query_budget
        and total_bytes < settings.overpass_daily_mb_budget * 1_000_000
    )


def build_query(bbox: BBox, timeout_seconds: int) -> str:
    """One Overpass QL query for every category in the bbox.

    `nwr` matches nodes, ways and relations; `out center tags` returns a
    centre point for ways/relations instead of their full geometry, which
    keeps responses small.
    """
    # Overpass bboxes are (south,west,north,east).
    area = f"({bbox.south},{bbox.west},{bbox.north},{bbox.east})"
    values_by_key: dict[str, list[str]] = defaultdict(list)
    for key, value in CATEGORIES.values():
        values_by_key[key].append(value)

    statements = []
    for key, values in sorted(values_by_key.items()):
        if len(values) == 1:
            statements.append(f'  nwr["{key}"="{values[0]}"]{area};')
        else:
            pattern = "|".join(sorted(values))
            statements.append(f'  nwr["{key}"~"^({pattern})$"]{area};')

    body = "\n".join(statements)
    return f"[out:json][timeout:{timeout_seconds}];\n(\n{body}\n);\nout center tags;"


def parse_response(payload: dict[str, Any]) -> list[OverpassElement]:
    """Turn Overpass JSON into elements, skipping anything we can't place."""
    elements: list[OverpassElement] = []
    for raw in payload["elements"]:
        tags: dict[str, Any] = raw.get("tags") or {}
        category = category_of(tags)
        point = raw if raw["type"] == "node" else raw.get("center")
        if category is None or not point or "lat" not in point:
            continue
        elements.append(
            OverpassElement(
                osm_type=raw["type"],
                osm_id=int(raw["id"]),
                lat=float(point["lat"]),
                lon=float(point["lon"]),
                category=category,
                name=tags.get("name"),
                tags=tags,
            )
        )
    return elements
