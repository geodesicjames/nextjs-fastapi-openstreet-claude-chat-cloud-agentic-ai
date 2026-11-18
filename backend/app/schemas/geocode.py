"""Responses for place-name search."""

from pydantic import BaseModel


class GeocodeHit(BaseModel):
    name: str
    display_name: str
    lon: float
    lat: float
    # [west, south, east, north]: fit the map to this.
    bbox: tuple[float, float, float, float]
    # OSM category and type, e.g. "place:city" or "highway:residential".
    kind: str


class GeocodeOut(BaseModel):
    results: list[GeocodeHit]
    # True when Nominatim couldn't be asked and these are older cached results.
    stale: bool
