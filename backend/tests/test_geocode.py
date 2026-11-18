"""Place-name search: Nominatim usage policy, caching, fallbacks and limits.

Nominatim is never called for real; respx returns canned responses and
records each request, so the tests can check spacing and call counts.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import GeocodeResult, UpstreamCall
from app.services import geocoding
from app.services.geocoding import normalize_query, parse_results

NOMINATIM = get_settings().nominatim_url

EDINBURGH = {
    "place_id": 1,
    "lat": "55.9533456",
    "lon": "-3.1883749",
    "category": "boundary",
    "type": "administrative",
    "name": "City of Edinburgh",
    "display_name": "City of Edinburgh, Scotland, United Kingdom",
    "boundingbox": ["55.818", "56.003", "-3.449", "-3.077"],  # S, N, W, E
}


@pytest.fixture
def nominatim(respx_mock: respx.MockRouter) -> respx.Route:
    return respx_mock.get(NOMINATIM).mock(return_value=httpx.Response(200, json=[EDINBURGH]))


async def geocode(client: AsyncClient, q: str) -> httpx.Response:
    return await client.get("/geocode", params={"q": q})


# --- Pure functions -----------------------------------------------------------------


def test_normalize_query() -> None:
    assert normalize_query("  Leith \t Walk  ") == "leith walk"


def test_parse_results_reorders_bbox_and_falls_back_to_display_name() -> None:
    unnamed = {**EDINBURGH, "name": ""}
    [named, fallback] = parse_results([EDINBURGH, unnamed])

    assert named == {
        "name": "City of Edinburgh",
        "display_name": "City of Edinburgh, Scotland, United Kingdom",
        "lon": -3.1883749,
        "lat": 55.9533456,
        "bbox": [-3.449, 55.818, -3.077, 56.003],  # W, S, E, N
        "kind": "boundary:administrative",
    }
    assert fallback["name"] == "City of Edinburgh"


# --- Asking Nominatim and caching -----------------------------------------------------


async def test_search_follows_usage_policy_and_returns_results(
    client: AsyncClient, db: AsyncSession, nominatim: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(osm_contact="https://example.com/contact")
    response = await geocode(client, "  Edinburgh ")

    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is False
    assert [r["name"] for r in body["results"]] == ["City of Edinburgh"]
    assert body["results"][0]["bbox"] == [-3.449, 55.818, -3.077, 56.003]

    request = nominatim.calls.last.request
    assert request.url.params["q"] == "edinburgh"
    assert request.url.params["format"] == "jsonv2"
    assert request.url.params["limit"] == "5"
    assert request.headers["user-agent"] == "PlacesPortfolioApp/0.1 (+https://example.com/contact)"
    call = await db.scalar(select(UpstreamCall).where(UpstreamCall.service == "nominatim"))
    assert call is not None and call.status_code == 200


async def test_same_query_is_answered_from_cache(
    client: AsyncClient, nominatim: respx.Route
) -> None:
    for q in ["Edinburgh", "edinburgh", "  EDINBURGH  "]:
        assert (await geocode(client, q)).status_code == 200

    assert nominatim.call_count == 1


async def test_no_results_are_cached_too(client: AsyncClient, nominatim: respx.Route) -> None:
    nominatim.return_value = httpx.Response(200, json=[])

    for _ in range(2):
        response = await geocode(client, "xyzzy nowhere")
        assert response.json() == {"results": [], "stale": False}

    assert nominatim.call_count == 1


async def test_stale_cache_is_refreshed(
    client: AsyncClient, db: AsyncSession, nominatim: respx.Route
) -> None:
    old = datetime.now(UTC) - timedelta(days=31)
    db.add(GeocodeResult(query="edinburgh", results=[], fetched_at=old))
    await db.commit()

    body = (await geocode(client, "Edinburgh")).json()

    assert nominatim.call_count == 1
    assert body["stale"] is False
    assert len(body["results"]) == 1
    refreshed = await db.get(GeocodeResult, "edinburgh", populate_existing=True)
    assert refreshed is not None and refreshed.fetched_at > old


async def test_stale_cache_is_served_when_nominatim_fails(
    client: AsyncClient, db: AsyncSession, nominatim: respx.Route
) -> None:
    old_results = parse_results([EDINBURGH])
    db.add(
        GeocodeResult(
            query="edinburgh",
            results=old_results,
            fetched_at=datetime.now(UTC) - timedelta(days=31),
        )
    )
    await db.commit()
    nominatim.return_value = httpx.Response(500)

    body = (await geocode(client, "Edinburgh")).json()

    assert body["stale"] is True
    assert body["results"] == old_results


async def test_unavailable_without_cache_is_503(
    client: AsyncClient, nominatim: respx.Route
) -> None:
    nominatim.return_value = httpx.Response(500)

    response = await geocode(client, "Edinburgh")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"


async def test_malformed_response_is_503(client: AsyncClient, nominatim: respx.Route) -> None:
    nominatim.return_value = httpx.Response(200, json=[{"unexpected": True}])
    assert (await geocode(client, "Edinburgh")).status_code == 503


# --- Staying within the usage policy -------------------------------------------------------


async def test_requests_are_at_least_one_second_apart(
    client: AsyncClient, nominatim: respx.Route
) -> None:
    times: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        times.append(time.monotonic())
        return httpx.Response(200, json=[EDINBURGH])

    nominatim.side_effect = respond
    for q in ["Edinburgh", "Glasgow", "Leith"]:  # distinct, so none is cached
        assert (await geocode(client, q)).status_code == 200

    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    assert len(gaps) == 2
    assert all(gap >= geocoding.MIN_INTERVAL_SECONDS for gap in gaps), gaps


@pytest.mark.parametrize("status", [429, 503])
async def test_slow_down_response_starts_a_cooldown(
    client: AsyncClient, nominatim: respx.Route, status: int
) -> None:
    nominatim.return_value = httpx.Response(status)

    assert (await geocode(client, "Edinburgh")).status_code == 503
    assert (await geocode(client, "Glasgow")).status_code == 503

    assert nominatim.call_count == 1  # no request during the cooldown


async def test_timeout_starts_a_cooldown(
    client: AsyncClient, db: AsyncSession, nominatim: respx.Route
) -> None:
    nominatim.side_effect = httpx.ReadTimeout("slow")

    assert (await geocode(client, "Edinburgh")).status_code == 503
    assert (await geocode(client, "Glasgow")).status_code == 503

    assert nominatim.call_count == 1
    call = await db.scalar(select(UpstreamCall))
    assert call is not None and call.status_code is None


async def test_daily_budget(
    client: AsyncClient, db: AsyncSession, nominatim: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(nominatim_daily_query_budget=1)

    assert (await geocode(client, "Edinburgh")).status_code == 200
    assert (await geocode(client, "Glasgow")).status_code == 503
    # Cached answers don't need the budget.
    assert (await geocode(client, "Edinburgh")).status_code == 200

    assert nominatim.call_count == 1
    assert await db.scalar(select(func.count()).select_from(UpstreamCall)) == 1


async def test_busy_when_queue_wait_runs_out(
    app: FastAPI, client: AsyncClient, nominatim: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(geocoding, "QUEUE_WAIT_SECONDS", 0.01)
    await app.state.nominatim.lock.acquire()  # another search is in progress
    try:
        response = await geocode(client, "Edinburgh")
    finally:
        app.state.nominatim.lock.release()

    assert response.status_code == 503
    assert nominatim.call_count == 0


# --- Validation and rate limiting -------------------------------------------------------------


@pytest.mark.parametrize("q", ["E", "   ", "x" * 201])
async def test_invalid_query_is_422(client: AsyncClient, nominatim: respx.Route, q: str) -> None:
    assert (await geocode(client, q)).status_code == 422
    assert nominatim.call_count == 0


async def test_geocode_has_its_own_rate_limit(
    client: AsyncClient, nominatim: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(geocode_rate_limit_per_minute=2, places_rate_limit_per_minute=100)
    headers = {"x-forwarded-for": "203.0.113.7"}

    statuses = [
        (await client.get("/geocode", params={"q": "Edinburgh"}, headers=headers)).status_code
        for _ in range(3)
    ]

    assert statuses == [200, 200, 429]
