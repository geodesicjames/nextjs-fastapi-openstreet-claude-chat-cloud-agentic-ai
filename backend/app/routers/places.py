"""Public place search for the map. No sign-in needed; rate-limited per IP."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.config import Settings, get_settings
from app.deps import DbSession
from app.ratelimit import RateLimit
from app.schemas.places import PlaceCollection, PlaceDetail, PlaceFeature
from app.services import places as service
from app.services.categories import CATEGORIES
from app.services.grid import MAX_CELLS, BBox, cell_count
from app.services.overpass import OverpassClient

router = APIRouter(prefix="/places", tags=["places"], dependencies=[Depends(RateLimit("places"))])


def get_overpass(request: Request) -> OverpassClient:
    return request.app.state.overpass


@router.get("", response_model=PlaceCollection)
async def search_places(
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    overpass: Annotated[OverpassClient, Depends(get_overpass)],
    bbox: Annotated[str, Query(description="west,south,east,north in degrees")],
    category: Annotated[
        list[str] | None,
        Query(description=f"Repeat to include several. One of: {', '.join(CATEGORIES)}"),
    ] = None,
) -> PlaceCollection:
    """Places in a bounding box, as GeoJSON.

    Served from the PostGIS cache; areas not fetched in the last
    PLACE_CACHE_DAYS are fetched from Overpass first, if the daily budget
    allows. If they can't be, the cached places are still returned, with
    `complete: false` and a reason.
    """
    try:
        area = BBox.parse(bbox)
    except ValueError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    if cell_count(area) > MAX_CELLS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Area too large. Zoom in to see places."
        )

    categories = category or list(CATEGORIES)
    unknown = sorted(set(categories) - set(CATEGORIES))
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown category: {', '.join(unknown)}"
        )

    result = await service.search_places(db, settings, overpass, area, categories)
    return PlaceCollection(
        features=[PlaceFeature.from_hit(hit) for hit in result.hits],
        complete=result.complete,
        incomplete_reason=result.incomplete_reason,
        truncated=result.truncated,
    )


@router.get("/{place_id}", response_model=PlaceDetail)
async def get_place(place_id: uuid.UUID, db: DbSession) -> PlaceDetail:
    """One place with all its OSM tags (opening hours, website, ...)."""
    hit = await service.get_place(db, place_id)
    if hit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Place not found.")
    return PlaceDetail.from_hit(hit)
