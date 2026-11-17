"""Shared pytest fixtures.

Tests run against a real Postgres + PostGIS database (DATABASE_URL), because
the point of most backend tests here is the SQL and spatial behaviour.
Locally that is the Docker Compose `db` service; in CI it is a service
container.

Each test runs inside a transaction that is rolled back afterwards, like
Django's TestCase, so tests never see each other's data and leave the
database clean.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import engine
from app.deps import get_db
from app.main import create_app
from app.services.email import get_email_sender


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
