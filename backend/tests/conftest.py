"""Shared pytest fixtures.

Tests run against a real Postgres + PostGIS database (DATABASE_URL), because
the point of most backend tests here is the SQL and spatial behaviour.
Locally that is the Docker Compose `db` service; in CI it is a service
container.

Like Django, tests never touch the database named in DATABASE_URL. They use
a sibling database with "_test" appended (app -> app_test), which is created
and migrated at the start of the run, so data from clicking around the dev app
can't leak into tests (or vice versa).

Each test runs inside a transaction that is rolled back afterwards, like
Django's TestCase, so tests never see each other's data and leave the test
database empty.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from alembic.config import Config
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.config import get_settings


def _test_database_url() -> URL:
    url = make_url(str(get_settings().database_url))
    if url.database is None:
        raise RuntimeError("DATABASE_URL must name a database")
    return url.set(database=f"{url.database}_test")


# Point the app at the test database *before* anything imports app.db, which
# builds its engine from settings at import time. Changing the env var and
# clearing the settings cache is the pydantic-settings equivalent of Django
# swapping in TEST_NAME.
TEST_DATABASE_URL = _test_database_url()
os.environ["DATABASE_URL"] = TEST_DATABASE_URL.render_as_string(hide_password=False)
get_settings.cache_clear()

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.db import engine  # noqa: E402
from app.deps import get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.email import get_email_sender  # noqa: E402


async def _create_test_database() -> None:
    """CREATE DATABASE <name>_test if it doesn't exist yet.

    CREATE DATABASE can't run inside a transaction, so this connects to the
    server's built-in `postgres` database in autocommit mode.
    """
    name = TEST_DATABASE_URL.database
    assert name is not None and name.endswith("_test"), name  # never touch a real DB
    admin = create_async_engine(
        TEST_DATABASE_URL.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Create the test database if needed and migrate it to head.

    The database is reused between runs (like Django's --keepdb); running the
    real migrations each time keeps its schema in step with alembic/versions.
    This hook is synchronous and runs before pytest-asyncio starts its event
    loop, so asyncio.run (here and inside alembic/env.py) is safe.
    """
    asyncio.run(_create_test_database())
    alembic_cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")


@dataclass
class SentEmail:
    to: str
    subject: str
    body: str


@dataclass
class FakeEmailSender:
    """Records emails instead of sending them (Django's `mail.outbox`)."""

    outbox: list[SentEmail] = field(default_factory=list)

    async def send(self, *, to: str, subject: str, body: str) -> None:
        self.outbox.append(SentEmail(to=to, subject=subject, body=body))


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A session bound to a transaction that is rolled back after the test.

    join_transaction_mode="create_savepoint" turns the app's own
    `session.commit()` calls into savepoint releases inside our outer
    transaction, so the code under test behaves normally but nothing is
    ever really committed.
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
def email_outbox() -> FakeEmailSender:
    return FakeEmailSender()


@pytest.fixture
async def client(db: AsyncSession, email_outbox: FakeEmailSender) -> AsyncIterator[AsyncClient]:
    """An HTTP client that calls the app in-process.

    ASGITransport sends requests straight to the ASGI app without opening a
    socket, much like Django's test Client. The base_url is arbitrary.
    Dependency overrides point the app at the test transaction and the fake
    email sender.
    """
    app = create_app()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_email_sender] = lambda: email_outbox

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
