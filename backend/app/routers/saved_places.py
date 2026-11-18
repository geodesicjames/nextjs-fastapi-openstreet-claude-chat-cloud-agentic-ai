"""The signed-in user's saved places (created by chat's save_place tool)."""

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from geoalchemy2.functions import ST_X, ST_Y
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.deps import CurrentUser, DbSession
from app.models import Place, SavedPlace

router = APIRouter(prefix="/saved-places", tags=["saved places"])


class SavedPlaceOut(BaseModel):
    id: uuid.UUID
    place_id: uuid.UUID
    name: str | None
    category: str
    lat: float
    lon: float
    note: str
    created_at: datetime


@router.get("", response_model=list[SavedPlaceOut])
async def list_saved_places(db: DbSession, user: CurrentUser) -> list[SavedPlaceOut]:
    """Newest first."""
    rows = await db.execute(
        select(SavedPlace, Place, ST_X(Place.geom), ST_Y(Place.geom))
        .join(Place, Place.id == SavedPlace.place_id)
        .where(SavedPlace.user_id == user.id)
        .order_by(SavedPlace.created_at.desc())
    )
    return [
        SavedPlaceOut(
            id=saved.id,
            place_id=place.id,
            name=place.name,
            category=place.category,
            lat=float(lat),
            lon=float(lon),
            note=saved.note,
            created_at=saved.created_at,
        )
        for saved, place, lon, lat in rows
    ]


@router.delete("/{saved_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_place(saved_id: uuid.UUID, db: DbSession, user: CurrentUser) -> None:
    deleted = await db.scalar(
        delete(SavedPlace)
        .where(SavedPlace.id == saved_id, SavedPlace.user_id == user.id)
        .returning(SavedPlace.id)
    )
    if deleted is None:  # missing, or someone else's: same answer
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Saved place not found.")
    await db.commit()
