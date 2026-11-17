"""Shared FastAPI dependencies.

A dependency is a function that FastAPI calls for you before running an
endpoint; the endpoint declares what it needs with `Depends(...)`:

    async def endpoint(db: AsyncSession = Depends(get_db)): ...

This replaces much of what Django does implicitly (request-scoped DB
connection) or with middleware/decorators (`request.user`,
`@login_required`). Because dependencies are resolved by FastAPI, tests can
swap any of them out with `app.dependency_overrides[get_db] = fake_get_db`.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import User
from app.services.auth import get_session_user
from app.services.chat_access import ChatStatus, Denial, get_chat_status


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a session for one request and always close it afterwards.

    Code before `yield` runs before the endpoint; the `async with` block's exit
    runs after the response is produced, even if the endpoint raised.
    """
    async with SessionLocal() as session:
        yield session


# `Annotated` aliases keep endpoint signatures short: `db: DbSession`.
DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_session_token(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> str | None:
    """The raw session token from the cookie, if any."""
    return request.cookies.get(settings.session_cookie_name) or None


SessionToken = Annotated[str | None, Depends(get_session_token)]


async def get_optional_user(db: DbSession, token: SessionToken) -> User | None:
    """The signed-in user, or None. For pages that work either way."""
    if token is None:
        return None
    return await get_session_user(db, token)


async def get_current_user(
    user: Annotated[User | None, Depends(get_optional_user)],
) -> User:
    """The signed-in user, or a 401. The `@login_required` equivalent."""
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    return user


OptionalUser = Annotated[User | None, Depends(get_optional_user)]
CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_chat_access(
    db: DbSession,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChatStatus:
    """Gate for every chat and agent route.

    401 if not signed in (via CurrentUser), 403 without an active grant in
    invite_only mode, 429 once today's token budget is spent. Routes receive
    the status, which carries the grant to log usage against.
    """
    chat_status = await get_chat_status(db, settings, user)
    if chat_status.denial is Denial.NO_GRANT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Chat is invite-only. Request access first.")
    if chat_status.denial is Denial.BUDGET_EXHAUSTED:
        seconds = int((chat_status.resets_at - datetime.now(UTC)).total_seconds()) + 1
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Daily chat budget used up. It resets at midnight UTC.",
            headers={"Retry-After": str(max(seconds, 1))},
        )
    return chat_status


ChatAccess = Annotated[ChatStatus, Depends(require_chat_access)]
