"""Helpers that create test data, shared between test modules.

Plain async functions rather than fixtures, so tests can call them with
arguments (like factory_boy factories in a Django project).
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ChatGrant, User
from app.services import auth as auth_service


async def make_user(db: AsyncSession, email: str, *, is_admin: bool = False) -> User:
    user = User(email=email, is_admin=is_admin)
    db.add(user)
    await db.flush()
    return user


async def sign_in_as(db: AsyncSession, client: AsyncClient, user: User) -> None:
    """Give `client` a session cookie for `user` (skips the email flow)."""
    settings = get_settings()
    token = await auth_service.create_session(db, settings, user)
    client.cookies.set(settings.session_cookie_name, token)


async def signed_in_user(
    db: AsyncSession,
    client: AsyncClient,
    email: str = "ada@example.com",
    *,
    is_admin: bool = False,
) -> User:
    user = await make_user(db, email, is_admin=is_admin)
    await sign_in_as(db, client, user)
    return user


async def grant(db: AsyncSession, user: User, **fields: object) -> ChatGrant:
    g = ChatGrant(user_id=user.id, **fields)
    db.add(g)
    await db.commit()
    return g
