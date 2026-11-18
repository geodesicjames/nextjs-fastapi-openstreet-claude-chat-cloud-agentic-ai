"""Place-name search ("geocoding") with Nominatim, within its usage policy.

Policy: https://operations.osmfoundation.org/policies/nominatim/
- at most 1 request per second, counting every user of the app together:
  enforced here by `NominatimClient`, which spaces requests at least
  MIN_INTERVAL_SECONDS apart (per backend process; with several processes the
  spacing would need a shared store, like the rate limiter);
- an identifying User-Agent (settings.user_agent);
- results must be cached, and the same query sent repeatedly may get a client
  blocked: every answer, including "no results", is cached for
  GEOCODE_CACHE_DAYS;
- no autocomplete: the API serves submitted searches only, and the per-IP
  rate limit keeps it that way.

Order of preference: fresh cache, then Nominatim, then stale cache. Only when
none is available is the search reported as unavailable.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import GeocodeResult
from app.services.upstream import BACKOFF_STATUSES, UpstreamClient, log_call, usage_today

logger = logging.getLogger(__name__)

SERVICE = "nominatim"
MIN_INTERVAL_SECONDS = 1.0
# How long a search waits for its turn before giving up as "busy".
QUEUE_WAIT_SECONDS = 5.0
MAX_RESULTS = 5

UnavailableReason = Literal["budget", "cooldown", "upstream_error", "busy"]


class GeocodingUnavailableError(Exception):
    def __init__(self, reason: UnavailableReason) -> None:
        super().__init__(reason)
        self.reason: UnavailableReason = reason


@dataclass
class NominatimClient(UpstreamClient):
    """Process-wide Nominatim state. One instance lives on `app.state`."""

    last_request_at: float = field(default=-MIN_INTERVAL_SECONDS)  # time.monotonic()

    async def search(
        self, db: AsyncSession, settings: Settings, query: str
    ) -> list[dict[str, Any]]:
        """Ask Nominatim, waiting for this process's 1-request-per-second slot.

        Raises GeocodingUnavailableError rather than exceeding the policy.
        """
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=QUEUE_WAIT_SECONDS)
        except TimeoutError:
            raise GeocodingUnavailableError("busy") from None
        try:
            if self.in_cooldown():
                raise GeocodingUnavailableError("cooldown")
            calls, _ = await usage_today(db, SERVICE)
            if calls >= settings.nominatim_daily_query_budget:
                raise GeocodingUnavailableError("budget")

            wait = self.last_request_at + MIN_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            return await self._request(db, settings, query)
        finally:
            # Spacing counts from when the request finished, which is stricter
            # than from when it started.
            self.last_request_at = time.monotonic()
            self.lock.release()

    async def _request(
        self, db: AsyncSession, settings: Settings, query: str
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        status: int | None = None
        size = 0
        try:
            response = await self.http.get(
                settings.nominatim_url,
                params={"q": query, "format": "jsonv2", "limit": MAX_RESULTS},
                headers={"User-Agent": settings.user_agent, "Accept-Language": "en"},
                timeout=settings.nominatim_timeout_seconds,
            )
            status, size = response.status_code, len(response.content)
        except httpx.HTTPError as error:
            seconds = self.back_off()
            logger.warning("Nominatim request failed: %r; pausing %ss", error, seconds)
            raise GeocodingUnavailableError("upstream_error") from error
        finally:
            await log_call(db, SERVICE, status, size, started)

        if status in BACKOFF_STATUSES:
            seconds = self.back_off()
            logger.warning("Nominatim returned %s; pausing %ss", status, seconds)
            raise GeocodingUnavailableError("upstream_error")
        if status != 200:
            logger.warning("Nominatim returned %s", status)
            raise GeocodingUnavailableError("upstream_error")

        self.succeeded()
        try:
            return parse_results(response.json())
        except (ValueError, KeyError, TypeError, IndexError) as error:
            logger.warning("Unexpected Nominatim response: %r", error)
            raise GeocodingUnavailableError("upstream_error") from error


def normalize_query(query: str) -> str:
    """Cache key: lower-case with whitespace collapsed ("  Leith  Walk" -> "leith walk")."""
    return re.sub(r"\s+", " ", query).strip().lower()


def parse_results(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the fields the map needs, in GeoJSON order (lon before lat).

    Nominatim's boundingbox is [south, north, west, east] as strings; ours is
    [west, south, east, north], the same order as the /places bbox.
    """
    results = []
    for item in payload:
        south, north, west, east = (float(v) for v in item["boundingbox"])
        results.append(
            {
                "name": item.get("name") or item["display_name"].split(",")[0],
                "display_name": item["display_name"],
                "lon": float(item["lon"]),
                "lat": float(item["lat"]),
                "bbox": [west, south, east, north],
                "kind": f"{item.get('category', '')}:{item.get('type', '')}",
            }
        )
    return results


@dataclass(frozen=True)
class GeocodeAnswer:
    results: list[dict[str, Any]]
    # False when Nominatim couldn't be asked and these are older cached results.
    fresh: bool


async def geocode(
    db: AsyncSession, settings: Settings, nominatim: NominatimClient, query: str
) -> GeocodeAnswer:
    """Results for `query`: fresh cache, else Nominatim, else stale cache.

    Raises GeocodingUnavailableError only when there's nothing cached at all.
    """
    key = normalize_query(query)
    now = datetime.now(UTC)
    # populate_existing: re-read the row even if this session already holds a
    # copy (the upsert below bypasses the ORM, so a held copy could be stale).
    cached = await db.get(GeocodeResult, key, populate_existing=True)
    if cached and cached.fetched_at > now - timedelta(days=settings.geocode_cache_days):
        return GeocodeAnswer(cached.results, fresh=True)

    try:
        results = await nominatim.search(db, settings, key)
    except GeocodingUnavailableError:
        if cached:
            return GeocodeAnswer(cached.results, fresh=False)
        raise

    statement = insert(GeocodeResult).values(query=key, results=results, fetched_at=now)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=["query"],
            set_={"results": statement.excluded.results, "fetched_at": now},
        )
    )
    await db.commit()
    return GeocodeAnswer(results, fresh=True)
