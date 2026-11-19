"""Tools Claude can call during a chat, backed by the app's own services.

Each tool has three parts:
- a definition sent to the API (name, a description that says *when* to call
  it, and a JSON Schema for its input);
- a Pydantic model that validates the input before anything runs;
- an async function that does the work and returns a `ToolOutcome`.

The definitions use `strict: true`, so the API guarantees inputs match the
schema's shape. Strict schemas can't express numeric or length bounds, so
those are stated in the descriptions and enforced by the Pydantic models;
an out-of-range value goes back to Claude as an error it can correct.

The tools reuse the same services as the map, so every limit already in
place applies to chat too: the Overpass and Nominatim budgets and caches,
and the per-user data boundaries (save_place only ever writes for the
signed-in user).
"""

import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from anthropic.types import ToolParam
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import SavedPlace, User
from app.services import geocoding, places
from app.services.categories import CATEGORIES
from app.services.geocoding import GeocodingUnavailableError, NominatimClient
from app.services.grid import MAX_CELLS, BBox, cell_count
from app.services.overpass import OverpassClient

MAX_PLACES_RETURNED = 20
MAX_TAGS_RETURNED = 40
METRES_PER_DEGREE = 111_320

Category = Literal["cafe", "restaurant", "park", "drinking_water", "toilets", "viewpoint"]


# --- Definitions sent to the API --------------------------------------------------------
# Kept in a fixed order with fixed text: they are part of the cached prompt
# prefix, so any change invalidates the cache.

TOOLS: list[ToolParam] = [
    {
        "name": "find_location",
        "description": (
            "Look up a town, neighbourhood, street, landmark or address and get its "
            "coordinates. Call this first whenever the user names a place, before "
            "search_places. Returns up to 5 matches, best first."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The place to look up, e.g. 'Leith Walk, Edinburgh'. 2-200 characters."
                    ),
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_places",
        "description": (
            "Find places of the given categories within a radius of a point, nearest "
            "first. Call this when the user asks what is near somewhere, or for "
            "recommendations of a kind of place. Only recommend places this returns. "
            "To find one specific place the user names (e.g. to look up its details), "
            "pass its name and a generous radius such as 1000."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude of the centre, -90 to 90."},
                "lon": {"type": "number", "description": "Longitude of the centre, -180 to 180."},
                "radius_m": {
                    "type": "integer",
                    "description": (
                        "Search radius in metres, 50 to 2000. Use about 500 for walking distance."
                    ),
                },
                "categories": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(CATEGORIES)},
                    "description": "One or more categories to include.",
                },
                "name": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": (
                        "Part of a place's name to match, ignoring case and accents "
                        "(e.g. 'whispering kettle'), up to 100 characters. "
                        "null to include every place."
                    ),
                },
            },
            "required": ["lat", "lon", "radius_m", "categories", "name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_place_details",
        "description": (
            "Get everything known about one place from search_places: opening hours, "
            "address, website, cuisine, accessibility and other OpenStreetMap tags. "
            "Call this before answering questions about a specific place."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "place_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "A place_id from search_places.",
                }
            },
            "required": ["place_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "save_place",
        "description": (
            "Save a place to the user's list, with an optional note. Only call this "
            "when the user asks to save, bookmark or remember a place."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "place_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "A place_id from search_places.",
                },
                "note": {
                    "type": "string",
                    "description": (
                        "Short note in the user's words, or an empty string. Up to 500 characters."
                    ),
                },
            },
            "required": ["place_id", "note"],
            "additionalProperties": False,
        },
    },
]


# --- Input validation ----------------------------------------------------------------------


class FindLocationIn(BaseModel):
    query: str = Field(min_length=2, max_length=200)


class SearchPlacesIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: int = Field(ge=50, le=2000)
    categories: list[Category] = Field(min_length=1)
    name: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def _blank_name_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class PlaceIdIn(BaseModel):
    place_id: uuid.UUID


class SavePlaceIn(PlaceIdIn):
    note: str = Field(max_length=500)


# --- Running tools ---------------------------------------------------------------------------


@dataclass
class ToolContext:
    db: AsyncSession
    settings: Settings
    user: User
    overpass: OverpassClient
    nominatim: NominatimClient


@dataclass
class ToolOutcome:
    """What a tool produced: JSON text for Claude, plus data for the UI."""

    content: str  # sent to Claude as the tool_result
    is_error: bool = False
    # A one-line description for the chat UI ("Found 12 places").
    summary: str = ""
    # Places to show on the map (id, name, category, lat, lon).
    places: list[dict[str, Any]] = field(default_factory=list)


def _ok(
    data: dict[str, Any], summary: str, places_: list[dict[str, Any]] | None = None
) -> ToolOutcome:
    return ToolOutcome(json.dumps(data, ensure_ascii=False), summary=summary, places=places_ or [])


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(json.dumps({"error": message}), is_error=True, summary=message)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine), accurate to well under 1% at these scales."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


async def _find_location(ctx: ToolContext, args: FindLocationIn) -> ToolOutcome:
    try:
        answer = await geocoding.geocode(ctx.db, ctx.settings, ctx.nominatim, args.query)
    except GeocodingUnavailableError:
        return _error("Location search is temporarily unavailable.")
    matches = [
        {
            "name": r["name"],
            "display_name": r["display_name"],
            "lat": r["lat"],
            "lon": r["lon"],
            "kind": r["kind"],
        }
        for r in answer.results
    ]
    if not matches:
        return _ok({"matches": []}, f"No match for “{args.query}”")
    return _ok({"matches": matches}, f"Found {matches[0]['name']}")


async def _search_places(ctx: ToolContext, args: SearchPlacesIn) -> ToolOutcome:
    dlat = args.radius_m / METRES_PER_DEGREE
    dlon = args.radius_m / (METRES_PER_DEGREE * max(math.cos(math.radians(args.lat)), 0.01))
    try:
        bbox = BBox(
            west=max(args.lon - dlon, -180),
            south=max(args.lat - dlat, -90),
            east=min(args.lon + dlon, 180),
            north=min(args.lat + dlat, 90),
        )
    except ValueError:
        return _error("That area can't be searched.")
    if cell_count(bbox) > MAX_CELLS:
        return _error("Search radius too large here; use a smaller radius_m.")

    result = await places.search_places(
        ctx.db,
        ctx.settings,
        ctx.overpass,
        bbox,
        list(args.categories),
        name=args.name,
        nearest_to=(args.lat, args.lon),
    )
    nearby = sorted(
        ((distance_m(args.lat, args.lon, hit.lat, hit.lon), hit) for hit in result.hits),
        key=lambda pair: pair[0],
    )
    nearby = [(d, hit) for d, hit in nearby if d <= args.radius_m]
    shown = nearby[:MAX_PLACES_RETURNED]
    found = [
        {
            "place_id": str(hit.place.id),
            "name": hit.place.name,
            "category": hit.place.category,
            "distance_m": round(d),
            "lat": round(hit.lat, 6),
            "lon": round(hit.lon, 6),
        }
        for d, hit in shown
    ]
    data: dict[str, Any] = {"places": found, "total_found": len(nearby)}
    if args.name and not found:
        data["note"] = (
            f"No {'/'.join(args.categories)} matching '{args.name}' within {args.radius_m} m. "
            "Try another spelling, a larger radius or other categories."
        )
    if not result.complete:
        # Tell Claude, so it can tell the user the list may be missing places.
        missing = "Some of this area couldn't be loaded right now, so places may be missing."
        data["note"] = f"{data['note']} {missing}" if "note" in data else missing
    map_places = [{k: p[k] for k in ("place_id", "name", "category", "lat", "lon")} for p in found]
    summary = f"Found {len(nearby)} place{'s' if len(nearby) != 1 else ''}"
    return _ok(data, summary, map_places)


async def _get_place_details(ctx: ToolContext, args: PlaceIdIn) -> ToolOutcome:
    hit = await places.get_place(ctx.db, args.place_id)
    if hit is None:
        return _error("No place with that place_id.")
    tags = dict(list(sorted(hit.place.tags.items()))[:MAX_TAGS_RETURNED])
    data = {
        "place_id": str(hit.place.id),
        "name": hit.place.name,
        "category": hit.place.category,
        "lat": round(hit.lat, 6),
        "lon": round(hit.lon, 6),
        "tags": tags,
        "osm_url": f"https://www.openstreetmap.org/{hit.place.osm_type}/{hit.place.osm_id}",
    }
    place = {
        "place_id": data["place_id"],
        "name": hit.place.name,
        "category": hit.place.category,
        "lat": data["lat"],
        "lon": data["lon"],
    }
    return _ok(data, f"Looked up {hit.place.name or 'a place'}", [place])


async def _save_place(ctx: ToolContext, args: SavePlaceIn) -> ToolOutcome:
    hit = await places.get_place(ctx.db, args.place_id)
    if hit is None:
        return _error("No place with that place_id.")
    statement = insert(SavedPlace).values(
        user_id=ctx.user.id, place_id=hit.place.id, note=args.note.strip()
    )
    # Saving again updates the note rather than failing.
    await ctx.db.execute(
        statement.on_conflict_do_update(
            constraint="uq_saved_places_user_place", set_={"note": statement.excluded.note}
        )
    )
    await ctx.db.flush()
    return _ok({"saved": True, "name": hit.place.name}, f"Saved {hit.place.name or 'the place'}")


_HANDLERS = {
    "find_location": (FindLocationIn, _find_location),
    "search_places": (SearchPlacesIn, _search_places),
    "get_place_details": (PlaceIdIn, _get_place_details),
    "save_place": (SavePlaceIn, _save_place),
}


async def run_tool(ctx: ToolContext, name: str, raw_input: object) -> ToolOutcome:
    """Validate and run one tool call. Never raises for bad input."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown tool: {name}")
    model, run = handler
    try:
        args = model.model_validate(raw_input)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in error.errors()
        )
        return _error(f"Invalid input ({problems})")
    return await run(ctx, args)
