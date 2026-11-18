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

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.categories import CATEGORIES, category_of
from app.services.grid import BBox
from app.services.upstream import BACKOFF_STATUSES, UpstreamClient, log_call, usage_today

logger = logging.getLogger(__name__)

SERVICE = "overpass"


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
class OverpassClient(UpstreamClient):
    """Process-wide Overpass state. One instance lives on `app.state`."""

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
            await log_call(db, SERVICE, status, size, started)

        if status in BACKOFF_STATUSES:
            seconds = self.back_off()
            logger.warning("Overpass returned %s; pausing %ss", status, seconds)
            raise OverpassUnavailableError("upstream_error")
        if status != 200:
            logger.warning("Overpass returned %s", status)
            raise OverpassUnavailableError("upstream_error")

        self.succeeded()
        try:
            return parse_response(response.json())
        except (ValueError, KeyError, TypeError) as error:
            logger.warning("Unexpected Overpass response: %r", error)
            raise OverpassUnavailableError("upstream_error") from error


async def within_budget(db: AsyncSession, settings: Settings) -> bool:
    """True if today's (UTC) Overpass calls and bytes are under budget."""
    calls, total_bytes = await usage_today(db, SERVICE)
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
