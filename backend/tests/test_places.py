"""Place search: grid caching, Overpass usage limits, GeoJSON output, rate limits.

Overpass is never called for real: respx intercepts the app's httpx requests
and returns canned responses, so these tests also count exactly how many
upstream calls each scenario makes.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Place, PlaceCell, UpstreamCall
from app.services import places as places_service
from app.services.grid import BBox, cell_count, cells_for, covering_bbox
from app.services.overpass import OverpassClient, build_query, parse_response

OVERPASS = get_settings().overpass_url

# A small area in central Edinburgh that sits inside a single grid cell
# (values chosen away from cell boundaries).
BBOX = "-3.195,55.945,-3.185,55.955"


def element(
    osm_id: int, tags: dict[str, str], *, lat: float = 55.95, lon: float = -3.19, way: bool = False
) -> dict[str, Any]:
    if way:
        return {"type": "way", "id": osm_id, "center": {"lat": lat, "lon": lon}, "tags": tags}
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags}


CAFE = element(1, {"amenity": "cafe", "name": "Bean There"})
PARK = element(2, {"leisure": "park", "name": "The Meadows"}, lat=55.941, lon=-3.191, way=True)
TOILETS = element(3, {"amenity": "toilets"}, lat=55.951, lon=-3.188)
BENCH = element(4, {"amenity": "bench"})  # not a category we show


def overpass_ok(*elements: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"elements": list(elements)})


@pytest.fixture
def overpass(respx_mock: respx.MockRouter) -> respx.Route:
    """The mocked Overpass endpoint; set `.return_value` / `.side_effect`."""
    return respx_mock.post(OVERPASS).mock(return_value=overpass_ok(CAFE, PARK, TOILETS, BENCH))


async def search(client: AsyncClient, bbox: str = BBOX, **params: Any) -> dict[str, Any]:
    response = await client.get("/places", params={"bbox": bbox, **params})
    assert response.status_code == 200, response.text
    return response.json()


def names(body: dict[str, Any]) -> list[str | None]:
    return [f["properties"]["name"] for f in body["features"]]


# --- Grid and query building (pure functions) --------------------------------


def test_bbox_parse_rejects_bad_input() -> None:
    for bad in ["1,2,3", "a,b,c,d", "10,0,5,1", "0,10,1,5", "0,0,181,1", "nan,0,1,1"]:
        with pytest.raises(ValueError):
            BBox.parse(bad)


def test_cells_cover_bbox_and_edges_are_exclusive() -> None:
    assert len(cells_for(BBox.parse(BBOX))) == 1
    # Exactly two cells wide: ending on a boundary doesn't add a third column.
    two = BBox(west=0.0, south=0.001, east=0.04, north=0.019)
    assert cells_for(two) == {(0, 0), (1, 0)}
    assert cell_count(two) == 2
    assert covering_bbox({(0, 0), (1, 1)}) == BBox(0.0, 0.0, 0.04, 0.04)


def test_query_includes_every_category_in_overpass_bbox_order() -> None:
    query = build_query(BBox(west=1.0, south=2.0, east=3.0, north=4.0), timeout_seconds=25)

    assert query.startswith("[out:json][timeout:25];")
    assert '["leisure"="park"](2.0,1.0,4.0,3.0)' in query  # south,west,north,east
    assert '["amenity"~"^(cafe|drinking_water|restaurant|toilets)$"]' in query
    assert '["tourism"="viewpoint"]' in query
    assert query.endswith("out center tags;")


def test_parse_response_uses_way_centres_and_skips_unknown() -> None:
    parsed = parse_response({"elements": [CAFE, PARK, BENCH, {"type": "way", "id": 9}]})

    assert [(e.osm_type, e.category) for e in parsed] == [("node", "cafe"), ("way", "park")]
    assert (parsed[1].lat, parsed[1].lon) == (55.941, -3.191)


# --- Fetching and caching -------------------------------------------------------


async def test_first_search_fetches_and_returns_geojson(
    client: AsyncClient, db: AsyncSession, overpass: respx.Route
) -> None:
    body = await search(client)

    assert overpass.call_count == 1
    assert body["type"] == "FeatureCollection"
    assert body["complete"] is True
    assert body["incomplete_reason"] is None
    assert body["truncated"] is False
    # Named places first (alphabetically), unnamed last. The park's centre is
    # outside the bbox, so it isn't returned even though it was fetched.
    assert names(body) == ["Bean There", None]
    cafe = body["features"][0]
    assert cafe["geometry"] == {"type": "Point", "coordinates": [-3.19, 55.95]}
    assert cafe["properties"] == {
        "name": "Bean There",
        "category": "cafe",
        "osm_type": "node",
        "osm_id": 1,
    }

    # Everything Overpass returned (except the bench) was stored.
    assert await db.scalar(select(func.count()).select_from(Place)) == 3
    assert await db.scalar(select(func.count()).select_from(PlaceCell)) == 1
    call = await db.scalar(select(UpstreamCall))
    assert call is not None and call.service == "overpass" and call.status_code == 200
    assert call.response_bytes > 0


async def test_query_identifies_the_app(
    client: AsyncClient, overpass: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(osm_contact="https://example.com/contact")
    await search(client)

    request = overpass.calls.last.request
    assert request.headers["user-agent"] == "PlacesPortfolioApp/0.1 (+https://example.com/contact)"
    query = parse_qs(request.content.decode())["data"][0]
    assert "(55.94,-3.2,55.96,-3.18)" in query  # the covering cell, not just the bbox


async def test_cached_area_is_not_fetched_again(client: AsyncClient, overpass: respx.Route) -> None:
    await search(client)
    await search(client)
    await search(client, category="cafe")

    assert overpass.call_count == 1


async def test_category_filter(client: AsyncClient, overpass: respx.Route) -> None:
    body = await search(client, category=["toilets", "park"])
    assert [f["properties"]["category"] for f in body["features"]] == ["toilets"]


async def test_stale_cell_is_refetched_and_vanished_places_removed(
    client: AsyncClient, db: AsyncSession, overpass: respx.Route
) -> None:
    first = await search(client)
    cafe_id = first["features"][0]["id"]

    await db.execute(update(PlaceCell).values(fetched_at=datetime.now(UTC) - timedelta(days=31)))
    await db.commit()
    renamed = element(1, {"amenity": "cafe", "name": "Bean Here Now"})
    overpass.return_value = overpass_ok(renamed)  # the toilets are gone from OSM

    second = await search(client)

    assert overpass.call_count == 2
    assert names(second) == ["Bean Here Now"]
    assert second["features"][0]["id"] == cafe_id  # same row, updated in place


async def test_more_than_max_results_is_truncated(
    client: AsyncClient, overpass: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(places_service, "MAX_RESULTS", 1)
    body = await search(client)

    assert len(body["features"]) == 1
    assert body["truncated"] is True


# --- Staying within the Overpass usage policy ---------------------------------------


async def test_daily_query_budget_stops_fetching_but_serves_cache(
    client: AsyncClient, db: AsyncSession, overpass: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(overpass_daily_query_budget=1)
    await search(client)  # uses the one allowed call

    elsewhere = "-3.175,55.945,-3.165,55.955"  # a different, unfetched cell
    body = await search(client, bbox=elsewhere)

    assert overpass.call_count == 1
    assert body["complete"] is False
    assert body["incomplete_reason"] == "budget"
    # Cached areas are still served in full.
    assert (await search(client))["complete"] is True


async def test_daily_byte_budget_stops_fetching(
    client: AsyncClient, db: AsyncSession, overpass: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(overpass_daily_mb_budget=1.0)
    db.add(
        UpstreamCall(service="overpass", status_code=200, response_bytes=1_000_000, duration_ms=1)
    )
    await db.commit()

    body = await search(client)

    assert overpass.call_count == 0
    assert body["incomplete_reason"] == "budget"


async def test_yesterdays_calls_do_not_count(
    client: AsyncClient, db: AsyncSession, overpass: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(overpass_daily_query_budget=1)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    db.add(
        UpstreamCall(
            service="overpass",
            status_code=200,
            response_bytes=10,
            duration_ms=1,
            created_at=yesterday,
        )
    )
    await db.commit()

    assert (await search(client))["complete"] is True
    assert overpass.call_count == 1


@pytest.mark.parametrize("status", [406, 429, 502, 503, 504])
async def test_slow_down_response_starts_a_cooldown(
    client: AsyncClient, overpass: respx.Route, status: int
) -> None:
    overpass.return_value = httpx.Response(status)

    first = await search(client)
    second = await search(client)

    assert first["incomplete_reason"] == "upstream_error"
    assert second["incomplete_reason"] == "cooldown"
    assert overpass.call_count == 1  # no retry during the cooldown


async def test_cooldown_doubles_and_resets_after_success(
    app: FastAPI, client: AsyncClient, overpass: respx.Route
) -> None:
    overpass.return_value = httpx.Response(429)
    client_state = app.state.overpass

    await search(client)
    assert 29 < client_state.cooldown_until - time.monotonic() <= 30
    client_state.cooldown_until = 0  # pretend the cooldown has passed
    await search(client)
    assert 59 < client_state.cooldown_until - time.monotonic() <= 60

    client_state.cooldown_until = 0
    overpass.return_value = overpass_ok(CAFE)
    assert (await search(client))["complete"] is True
    assert client_state.consecutive_failures == 0


def test_cooldown_is_capped() -> None:
    state = OverpassClient(http=httpx.AsyncClient())
    seconds = [state.back_off() for _ in range(8)]
    assert seconds == [30, 60, 120, 240, 480, 600, 600, 600]


async def test_server_error_is_logged_and_reported(
    client: AsyncClient, db: AsyncSession, overpass: respx.Route
) -> None:
    overpass.return_value = httpx.Response(500)

    first = await search(client)
    second = await search(client)

    assert first["incomplete_reason"] == second["incomplete_reason"] == "upstream_error"
    assert overpass.call_count == 2  # a plain 500 doesn't trigger the cooldown
    assert await db.scalar(select(func.count()).select_from(PlaceCell)) == 0


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("unreachable")])
async def test_timeout_or_connection_error_starts_a_cooldown(
    client: AsyncClient, db: AsyncSession, overpass: respx.Route, error: httpx.HTTPError
) -> None:
    overpass.side_effect = error

    first = await search(client)
    second = await search(client)

    assert first["incomplete_reason"] == "upstream_error"
    assert second["incomplete_reason"] == "cooldown"
    assert overpass.call_count == 1
    call = await db.scalar(select(UpstreamCall))
    assert call is not None and call.status_code is None


async def test_malformed_response_is_upstream_error(
    client: AsyncClient, overpass: respx.Route
) -> None:
    overpass.return_value = httpx.Response(200, text="<html>Rate limited</html>")
    assert (await search(client))["incomplete_reason"] == "upstream_error"


async def test_waits_briefly_then_answers_busy_if_a_fetch_is_running(
    app: FastAPI, client: AsyncClient, overpass: respx.Route, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(places_service, "LOCK_WAIT_SECONDS", 0.01)
    await app.state.overpass.lock.acquire()  # simulate another request's fetch
    try:
        body = await search(client)
    finally:
        app.state.overpass.lock.release()

    assert body["incomplete_reason"] == "busy"
    assert overpass.call_count == 0


# --- Validation and detail ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"bbox": "1,2,3"}, "west,south,east,north"),
        ({"bbox": "-3.3,55.9,-3.1,56.0"}, "Zoom in"),  # 10 x 5 cells
        ({"bbox": BBOX, "category": "casino"}, "Unknown category: casino"),
    ],
)
async def test_invalid_search_is_422(
    client: AsyncClient, overpass: respx.Route, params: dict[str, str], detail: str
) -> None:
    response = await client.get("/places", params=params)

    assert response.status_code == 422
    assert detail in response.json()["detail"]
    assert overpass.call_count == 0


async def test_place_detail(client: AsyncClient, overpass: respx.Route) -> None:
    cafe_id = (await search(client))["features"][0]["id"]

    response = await client.get(f"/places/{cafe_id}")

    assert response.status_code == 200
    props = response.json()["properties"]
    assert props["tags"] == {"amenity": "cafe", "name": "Bean There"}
    assert props["osm_url"] == "https://www.openstreetmap.org/node/1"


async def test_unknown_place_is_404(client: AsyncClient) -> None:
    response = await client.get("/places/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


# --- Per-IP rate limiting ---------------------------------------------------------------


async def test_rate_limit_per_client_ip(
    client: AsyncClient, overpass: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(places_rate_limit_per_minute=2)
    # The test client connects from 127.0.0.1, a trusted proxy by default,
    # so X-Forwarded-For identifies the visitor (as with Next.js in front).
    alice = {"x-forwarded-for": "203.0.113.1"}
    bob = {"x-forwarded-for": "203.0.113.2"}

    for _ in range(2):
        assert (
            await client.get("/places", params={"bbox": BBOX}, headers=alice)
        ).status_code == 200
    blocked = await client.get("/places", params={"bbox": BBOX}, headers=alice)
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["retry-after"]) <= 60

    assert (await client.get("/places", params={"bbox": BBOX}, headers=bob)).status_code == 200


async def test_forwarded_for_is_ignored_from_untrusted_peers(
    client: AsyncClient, overpass: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(places_rate_limit_per_minute=1, trusted_proxies=[])
    # Without trust, a client can't reset its limit by inventing new IPs.
    first = await client.get(
        "/places", params={"bbox": BBOX}, headers={"x-forwarded-for": "1.1.1.1"}
    )
    second = await client.get(
        "/places", params={"bbox": BBOX}, headers={"x-forwarded-for": "2.2.2.2"}
    )

    assert first.status_code == 200
    assert second.status_code == 429


async def test_forwarded_chain_uses_rightmost_untrusted_address(
    client: AsyncClient, overpass: respx.Route, use_settings: Callable[..., None]
) -> None:
    use_settings(places_rate_limit_per_minute=1)
    # A client-supplied spoofed entry on the left must not be used as the key:
    # the proxy appended the real address (203.0.113.9) on the right.
    spoofed_1 = {"x-forwarded-for": "9.9.9.9, 203.0.113.9"}
    spoofed_2 = {"x-forwarded-for": "8.8.8.8, 203.0.113.9"}

    assert (
        await client.get("/places", params={"bbox": BBOX}, headers=spoofed_1)
    ).status_code == 200
    assert (
        await client.get("/places", params={"bbox": BBOX}, headers=spoofed_2)
    ).status_code == 429
