"""Shared pytest fixtures.

Tests run against a real Postgres + PostGIS database (DATABASE_URL), because
the point of most backend tests here is the SQL and spatial behaviour.
Locally that is the Docker Compose `db` service; in CI it is a service
container.
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client that calls the app in-process.

    ASGITransport sends requests straight to the ASGI app without opening a
    socket, much like Django's test Client. The base_url is arbitrary.
    """
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
