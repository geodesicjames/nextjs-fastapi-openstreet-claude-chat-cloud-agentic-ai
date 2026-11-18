"""Places cached from OpenStreetMap, and the bookkeeping that keeps us within
the public services' usage policies.

- `Place`: one OSM feature (node, way or relation) we show on the map.
  Ways and relations (e.g. a park's outline) are stored as their centre point.
- `PlaceCell`: which grid cells have been fetched from Overpass, and when.
  This is the cache index: a fresh cell is answered from PostGIS alone.
- `UpstreamCall`: one row per request we make to an external service, used to
  enforce the daily budgets (and handy for spotting trouble).
- `GeocodeResult`: cached Nominatim answers, keyed by normalised search text.
- `SavedPlace`: places a user has saved, with an optional note.
"""

import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Place(TimestampMixin, Base):
    __tablename__ = "places"
    __table_args__ = (UniqueConstraint("osm_type", "osm_id", name="uq_places_osm"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    osm_type: Mapped[str] = mapped_column(String(8))  # node | way | relation
    osm_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(50), index=True)
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # SRID 4326 = plain longitude/latitude (WGS 84), what GPS and OSM use.
    # GeoAlchemy2 creates a GiST spatial index for this column automatically.
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326))
    # When this row was last confirmed by an Overpass response. Rows in a
    # refreshed area that weren't in the response have been deleted from OSM.
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlaceCell(Base):
    """A grid cell (see app/services/grid.py) whose places we have fetched."""

    __tablename__ = "place_cells"

    ix: Mapped[int] = mapped_column(primary_key=True)
    iy: Mapped[int] = mapped_column(primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UpstreamCall(TimestampMixin, Base):
    __tablename__ = "upstream_calls"
    __table_args__ = (
        # Serves "how many Overpass calls today?"
        Index("ix_upstream_calls_service_created_at", "service", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    service: Mapped[str] = mapped_column(String(20))  # overpass | nominatim
    # NULL when the request never got a response (timeout, connection error).
    status_code: Mapped[int | None]
    response_bytes: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int]


class GeocodeResult(Base):
    """Cached Nominatim results for one normalised search string.

    Empty results are cached too: the usage policy warns that clients sending
    the same query repeatedly may be blocked.
    """

    __tablename__ = "geocode_cache"

    query: Mapped[str] = mapped_column(String(200), primary_key=True)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SavedPlace(TimestampMixin, Base):
    """A place a user saved (from chat's save_place tool, or later the map).

    Deleted together with the place if OpenStreetMap removes it (the place
    no longer exists, so there is nothing left to show).
    """

    __tablename__ = "saved_places"
    __table_args__ = (UniqueConstraint("user_id", "place_id", name="uq_saved_places_user_place"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # No separate index on user_id: the unique (user_id, place_id) constraint
    # starts with it, so it already serves "this user's saved places".
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    place_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("places.id", ondelete="CASCADE"))
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
