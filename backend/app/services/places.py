"""Place search: serve from PostGIS, filling gaps from Overpass when allowed.

For a bbox:
1. Work out which grid cells it covers, and which of those are stale or
   have never been fetched.
2. If any are, fetch the smallest bbox covering them from Overpass (one
   query, all categories), upsert the places, delete places in that area
   that OSM no longer has, and mark the cells fetched.
3. Answer from PostGIS either way.

If step 2 can't happen (daily budget used, Overpass busy or down), the
answer is whatever is cached, with `complete=False` and a reason, rather than
an error: the map still shows something, and the frontend can say why.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from geoalchemy2 import Geography
from geoalchemy2.functions import ST_X, ST_Y, ST_Intersects, ST_MakeEnvelope
from sqlalchemy import cast, delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Place, PlaceCell
from app.services.grid import BBox, Cell, cells_for, covering_bbox
from app.services.overpass import (
    OverpassClient,
    OverpassElement,
    OverpassUnavailableError,
    UnavailableReason,
)

IncompleteReason = UnavailableReason | Literal["busy"]

# Most places returned for one request. The map shows "zoom in" past this.
MAX_RESULTS = 500
# How long a request waits for another request's Overpass fetch to finish
# before answering from the cache alone.
LOCK_WAIT_SECONDS = 5


@dataclass(frozen=True)
class PlaceHit:
    place: Place
    lon: float
    lat: float


@dataclass(frozen=True)
class SearchResult:
    hits: list[PlaceHit]
    complete: bool
    incomplete_reason: IncompleteReason | None
    truncated: bool


def _now() -> datetime:
    return datetime.now(UTC)


def envelope(bbox: BBox) -> ST_MakeEnvelope:
    """The bbox as a PostGIS polygon (SRID 4326), for spatial filters."""
    return ST_MakeEnvelope(bbox.west, bbox.south, bbox.east, bbox.north, 4326)


async def stale_cells(db: AsyncSession, settings: Settings, cells: set[Cell]) -> set[Cell]:
    """The cells that have never been fetched or were fetched too long ago."""
    fresh_since = _now() - timedelta(days=settings.place_cache_days)
    fresh = await db.execute(
        select(PlaceCell.ix, PlaceCell.iy).where(
            tuple_(PlaceCell.ix, PlaceCell.iy).in_(cells),
            PlaceCell.fetched_at > fresh_since,
        )
    )
    return cells - {(ix, iy) for ix, iy in fresh}


async def save_fetch(
    db: AsyncSession, area: BBox, elements: list[OverpassElement], fetched_at: datetime
) -> None:
    """Store one Overpass response for `area`, replacing what was there."""
    if elements:
        statement = insert(Place).values(
            [
                {
                    "id": uuid.uuid4(),
                    "osm_type": e.osm_type,
                    "osm_id": e.osm_id,
                    "name": e.name,
                    "category": e.category,
                    "tags": e.tags,
                    # EWKT: "SRID=4326;POINT(lon lat)". Longitude comes first.
                    "geom": f"SRID=4326;POINT({e.lon} {e.lat})",
                    "fetched_at": fetched_at,
                }
                for e in elements
            ]
        )
        # Upsert on the OSM identity: existing rows keep their id (so links
        # to /places/{id} stay valid) and get the latest name/tags/position.
        await db.execute(
            statement.on_conflict_do_update(
                constraint="uq_places_osm",
                set_={
                    "name": statement.excluded.name,
                    "category": statement.excluded.category,
                    "tags": statement.excluded.tags,
                    "geom": statement.excluded.geom,
                    "fetched_at": statement.excluded.fetched_at,
                },
            )
        )

    # Anything in the area that this response didn't confirm is gone from OSM.
    await db.execute(
        delete(Place).where(
            ST_Intersects(Place.geom, envelope(area)), Place.fetched_at < fetched_at
        )
    )

    cells = cells_for(area)
    cell_rows = insert(PlaceCell).values(
        [{"ix": ix, "iy": iy, "fetched_at": fetched_at} for ix, iy in cells]
    )
    await db.execute(
        cell_rows.on_conflict_do_update(
            index_elements=["ix", "iy"], set_={"fetched_at": cell_rows.excluded.fetched_at}
        )
    )
    await db.commit()


async def refresh_cells(
    db: AsyncSession, settings: Settings, overpass: OverpassClient, cells: set[Cell]
) -> IncompleteReason | None:
    """Fetch stale cells from Overpass. Returns why it couldn't, or None."""
    try:
        await asyncio.wait_for(overpass.lock.acquire(), timeout=LOCK_WAIT_SECONDS)
    except TimeoutError:
        return "busy"
    try:
        # Another request may have fetched these while we waited for the lock.
        cells = await stale_cells(db, settings, cells)
        if not cells:
            return None
        area = covering_bbox(cells)
        fetched_at = _now()
        try:
            elements = await overpass.fetch(db, settings, area)
        except OverpassUnavailableError as error:
            return error.reason
        await save_fetch(db, area, elements, fetched_at)
        return None
    finally:
        overpass.lock.release()


async def search_places(
    db: AsyncSession,
    settings: Settings,
    overpass: OverpassClient,
    bbox: BBox,
    categories: list[str],
    *,
    name: str | None = None,
    nearest_to: tuple[float, float] | None = None,
) -> SearchResult:
    """Places of the given categories in `bbox`, fetching missing cells first.

    `name`: only places whose name contains it, ignoring case and accents
    ("soderberg" matches "Söderberg").
    `nearest_to`: (lat, lon) to order by real distance from, so that if
    there are more than MAX_RESULTS matches the nearest are kept. Without
    it, results are ordered by name (fine for the map, which shows them all).
    """
    reason: IncompleteReason | None = None
    missing = await stale_cells(db, settings, cells_for(bbox))
    if missing:
        reason = await refresh_cells(db, settings, overpass, missing)

    query = select(Place, ST_X(Place.geom), ST_Y(Place.geom)).where(
        ST_Intersects(Place.geom, envelope(bbox)), Place.category.in_(categories)
    )
    if name:
        query = query.where(
            func.unaccent(func.lower(Place.name)).like(
                func.unaccent(func.lower(f"%{_escape_like(name)}%")), escape="\\"
            )
        )
    if nearest_to is not None:
        lat, lon = nearest_to
        point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
        # Geography: distance in metres on the sphere, not distorted degrees.
        query = query.order_by(
            func.ST_Distance(cast(Place.geom, Geography), cast(point, Geography)), Place.id
        )
    else:
        query = query.order_by(Place.name.asc().nulls_last(), Place.id)
    rows = await db.execute(query.limit(MAX_RESULTS + 1))
    hits = [PlaceHit(place, float(lon), float(lat)) for place, lon, lat in rows]
    return SearchResult(
        hits=hits[:MAX_RESULTS],
        complete=reason is None,
        incomplete_reason=reason,
        truncated=len(hits) > MAX_RESULTS,
    )


def _escape_like(text: str) -> str:
    """Make `%`, `_` and the escape character itself match literally in LIKE."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def get_place(db: AsyncSession, place_id: uuid.UUID) -> PlaceHit | None:
    row = (
        await db.execute(
            select(Place, ST_X(Place.geom), ST_Y(Place.geom)).where(Place.id == place_id)
        )
    ).first()
    if row is None:
        return None
    place, lon, lat = row
    return PlaceHit(place, float(lon), float(lat))
