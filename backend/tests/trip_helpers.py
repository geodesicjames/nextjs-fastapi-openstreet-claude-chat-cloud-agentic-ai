"""Shared test data for the trip planner: places around Leith Walk, plans, scripts.

Used by the trip planner's own tests and the agent API tests.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GeocodeResult, Place, PlaceCell
from app.services.grid import BBox, cells_for
from tests.chat_helpers import THINKING, reply

LEITH = {"name": "Leith Walk", "lat": 55.9600, "lon": -3.1800}
SPOTS = {
    "cafe": {"name": "Bean There", "category": "cafe", "lat": 55.9610, "lon": -3.1800},  # ~111 m
    "park": {"name": "Pilrig Park", "category": "park", "lat": 55.9605, "lon": -3.1810},  # ~85 m
    "view": {"name": "Calton Hill", "category": "viewpoint", "lat": 55.9650, "lon": -3.1800},
    "far": {
        "name": "Far Feast",
        "category": "restaurant",
        "lat": 56.0100,
        "lon": -3.1800,
    },  # ~5.6 km
}


async def add_spots(db: AsyncSession) -> dict[str, Place]:
    """Put SPOTS in the database, with fresh cells and a cached geocode for Leith Walk."""
    now = datetime.now(UTC)
    rows: dict[str, Place] = {}
    for i, (key, p) in enumerate(SPOTS.items()):
        place = Place(
            osm_type="node",
            osm_id=2000 + i,
            name=p["name"],
            category=p["category"],
            tags={"name": p["name"]},
            geom=f"SRID=4326;POINT({p['lon']} {p['lat']})",
            fetched_at=now,
        )
        db.add(place)
        rows[key] = place
    area = BBox(west=-3.20, south=55.95, east=-3.16, north=55.97)
    db.add_all(PlaceCell(ix=ix, iy=iy, fetched_at=now) for ix, iy in cells_for(area))
    db.add(
        GeocodeResult(
            query="leith walk",
            results=[
                {
                    "name": "Leith Walk",
                    "display_name": "Leith Walk, Edinburgh, Scotland",
                    "lat": LEITH["lat"],
                    "lon": LEITH["lon"],
                    "bbox": [-3.19, 55.95, -3.17, 55.97],
                    "kind": "highway:primary",
                }
            ],
            fetched_at=now,
        )
    )
    await db.commit()
    return rows


def plan(*stops: tuple[Place, int], **fields: Any) -> dict[str, Any]:
    return {
        "title": "Coffee and a park",
        "summary": "A short loop from Leith Walk.",
        "categories": ["cafe", "park"],
        "start": LEITH,
        "return_to_start": True,
        "start_time": None,
        "end_by": None,
        "max_minutes": None,
        "stops": [
            {"place_id": str(place.id), "minutes": minutes, "why": "Close by."}
            for place, minutes in stops
        ],
    } | fields


def call(name: str, tool_input: dict[str, Any], id: str) -> list[Any]:
    block = {"type": "tool_use", "id": id, "name": name, "input": tool_input}
    return reply(content=[THINKING, block], stop_reason="tool_use")


SEARCH = {
    "lat": LEITH["lat"],
    "lon": LEITH["lon"],
    "radius_m": 800,
    "categories": ["cafe", "park"],
    "name": None,
}


def good_run_script(spots: dict[str, Place], prefix: str = "t") -> list[list[Any]]:
    """Model replies for a run that finds Leith Walk, searches, and submits a good plan."""
    return [
        call("find_location", {"query": "Leith Walk"}, f"{prefix}1"),
        call("search_places", SEARCH, f"{prefix}2"),
        call("submit_plan", plan((spots["cafe"], 30), (spots["park"], 20)), f"{prefix}3"),
    ]
