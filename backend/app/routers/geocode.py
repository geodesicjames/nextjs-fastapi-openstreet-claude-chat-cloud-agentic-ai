"""Public place-name search. Rate-limited per IP; submitted searches only.

Results come from Nominatim (https://nominatim.openstreetmap.org), used
according to its usage policy:
https://operations.osmfoundation.org/policies/nominatim/
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.config import Settings, get_settings
from app.deps import DbSession
from app.ratelimit import RateLimit
from app.schemas.geocode import GeocodeHit, GeocodeOut
from app.services.geocoding import GeocodingUnavailableError, NominatimClient, geocode

router = APIRouter(tags=["geocode"])


def get_nominatim(request: Request) -> NominatimClient:
    return request.app.state.nominatim


@router.get("/geocode", response_model=GeocodeOut, dependencies=[Depends(RateLimit("geocode"))])
async def search(
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    nominatim: Annotated[NominatimClient, Depends(get_nominatim)],
    q: Annotated[str, Query(min_length=2, max_length=200, description="Place name or address")],
) -> GeocodeOut:
    """Find a town, street or address. Up to 5 results, best match first.

    Call this when the user submits a search, never on each keystroke: the
    Nominatim policy forbids autocomplete.
    """
    if not q.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter a place to search for.")
    try:
        answer = await geocode(db, settings, nominatim, q)
    except GeocodingUnavailableError as error:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Search is temporarily unavailable. Please try again in a minute.",
            headers={"Retry-After": "60"},
        ) from error
    return GeocodeOut(
        results=[GeocodeHit.model_validate(r) for r in answer.results], stale=not answer.fresh
    )
