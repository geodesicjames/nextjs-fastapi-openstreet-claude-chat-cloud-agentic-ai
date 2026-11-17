"""Response schemas for the health check.

Pydantic schemas are the API contract, similar to DRF serializers. They are
kept separate from SQLAlchemy models so internal columns can never leak into a
response by accident.
"""

from typing import Literal

from pydantic import BaseModel


class DatabaseHealth(BaseModel):
    connected: bool
    postgis_version: str | None = None


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    database: DatabaseHealth
