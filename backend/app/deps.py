"""Shared FastAPI dependencies.

A dependency is a function that FastAPI calls for you before running an
endpoint; the endpoint declares what it needs with `Depends(...)`:

    async def endpoint(db: AsyncSession = Depends(get_db)): ...

This replaces much of what Django does implicitly (request-scoped DB
connection) or with middleware/decorators (current user, permissions). Because
dependencies are resolved by FastAPI, tests can swap any of them out with
`app.dependency_overrides[get_db] = fake_get_db`.

Later this module will also hold `get_current_user` and `require_chat_access`.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a session for one request and always close it afterwards.

    Code before `yield` runs before the endpoint; the `async with` block's exit
    runs after the response is produced, even if the endpoint raised.
    """
    async with SessionLocal() as session:
        yield session


# `Annotated` aliases keep endpoint signatures short: `db: DbSession`.
DbSession = Annotated[AsyncSession, Depends(get_db)]
