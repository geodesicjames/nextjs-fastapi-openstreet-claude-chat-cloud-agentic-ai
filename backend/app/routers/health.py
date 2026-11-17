"""Health check endpoint.

An APIRouter groups related endpoints, roughly a Django app's urls.py and
views.py in one place. It is attached to the app in `app/main.py`.
"""

import logging

from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.deps import DbSession
from app.schemas.health import DatabaseHealth, HealthOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(db: DbSession, response: Response) -> HealthOut:
    """Report API status and confirm the database (and PostGIS) is reachable.

    Returns 200 when everything is up, 503 when the database is not, so the
    endpoint works both for the frontend status page and as a healthcheck.
    """
    try:
        result = await db.execute(text("SELECT postgis_lib_version()"))
        postgis_version = result.scalar_one()
    except (SQLAlchemyError, OSError):
        logger.exception("Health check: database unavailable")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthOut(status="degraded", database=DatabaseHealth(connected=False))

    return HealthOut(
        status="ok",
        database=DatabaseHealth(connected=True, postgis_version=postgis_version),
    )
