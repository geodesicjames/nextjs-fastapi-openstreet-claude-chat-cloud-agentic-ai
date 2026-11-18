"""GeoJSON responses for places.

GeoJSON (RFC 7946) is what map libraries consume directly: MapLibre can add a
FeatureCollection as a source with no conversion. Extra top-level members
like `complete` are allowed by the spec ("foreign members").
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel

from app.services.places import IncompleteReason, PlaceHit


class PointGeometry(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float]  # (longitude, latitude), GeoJSON order


class PlaceProperties(BaseModel):
    name: str | None
    category: str
    osm_type: str
    osm_id: int


class PlaceDetailProperties(PlaceProperties):
    tags: dict[str, Any]
    osm_url: str


class PlaceFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: uuid.UUID
    geometry: PointGeometry
    properties: PlaceProperties

    @classmethod
    def from_hit(cls, hit: PlaceHit) -> "PlaceFeature":
        p = hit.place
        return cls(
            id=p.id,
            geometry=PointGeometry(coordinates=(hit.lon, hit.lat)),
            properties=PlaceProperties(
                name=p.name, category=p.category, osm_type=p.osm_type, osm_id=p.osm_id
            ),
        )


class PlaceDetail(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: uuid.UUID
    geometry: PointGeometry
    properties: PlaceDetailProperties

    @classmethod
    def from_hit(cls, hit: PlaceHit) -> "PlaceDetail":
        p = hit.place
        return cls(
            id=p.id,
            geometry=PointGeometry(coordinates=(hit.lon, hit.lat)),
            properties=PlaceDetailProperties(
                name=p.name,
                category=p.category,
                osm_type=p.osm_type,
                osm_id=p.osm_id,
                tags=p.tags,
                osm_url=f"https://www.openstreetmap.org/{p.osm_type}/{p.osm_id}",
            ),
        )


class PlaceCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[PlaceFeature]
    # False when some of the area couldn't be fetched (see incomplete_reason).
    complete: bool
    incomplete_reason: IncompleteReason | None
    # True when there were more than the maximum places; zoom in to see all.
    truncated: bool
