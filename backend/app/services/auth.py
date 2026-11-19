"""Magic-link sign-in and server-side sessions.

Flow:
1. `create_login_token(email)` stores a hashed single-use token and returns
   the raw token, which goes into the emailed link.
2. `consume_login_token(token)` checks and burns it, then returns the user,
   creating the account on first sign-in.
3. `create_session(user)` stores a hashed session token and returns the raw
   token, which the router puts in an httpOnly cookie.
4. `get_session_user(token)` resolves that cookie on later requests.

This module holds the logic; `app/routers/auth.py` only handles HTTP.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import LoginToken, User, UserSession
from app.security import generate_token, hash_token

# Updating last_seen_at on every request would turn every read into a write;
# once every few minutes is plenty.
LAST_SEEN_RESOLUTION = timedelta(minutes=5)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _now() -> datetime:
    return datetime.now(UTC)


async def create_login_token(db: AsyncSession, settings: Settings, email: str) -> str | None:
    """Create a login token for `email`, or return None if it is rate-limited."""
    email = normalize_email(email)
    now = _now()

    recent = await db.scalar(
        select(func.count())
        .select_from(LoginToken)
        .where(LoginToken.email == email, LoginToken.created_at > now - timedelta(hours=1))
    )
    if (recent or 0) >= settings.login_link_max_per_hour:
        return None

    token = generate_token()
    db.add(
        LoginToken(
            token_hash=hash_token(token),
            email=email,
            expires_at=now + timedelta(minutes=settings.login_link_lifetime_minutes),
        )
    )
    await db.commit()
    return token


async def consume_login_token(db: AsyncSession, token: str) -> User | None:
    """Burn a login token and return its user, or None if it is not valid."""
    now = _now()

    # FOR UPDATE locks the row, so two simultaneous clicks on the same link
    # cannot both succeed: the second waits, then sees used_at set.
    login_token = await db.scalar(
        select(LoginToken).where(LoginToken.token_hash == hash_token(token)).with_for_update()
    )
    if login_token is None or login_token.used_at is not None or login_token.expires_at <= now:
        return None

    login_token.used_at = now

    user = await db.scalar(select(User).where(User.email == login_token.email))
    if user is None:
        user = User(email=login_token.email)
        db.add(user)
    user.last_login_at = now

    await db.commit()
    return user


async def create_session(db: AsyncSession, settings: Settings, user: User) -> str:
    token = generate_token()
    now = _now()
    db.add(
        UserSession(
            token_hash=hash_token(token),
            user_id=user.id,
            expires_at=now + timedelta(days=settings.session_lifetime_days),
            last_seen_at=now,
        )
    )
    await db.commit()
    return token


async def get_session_user(db: AsyncSession, token: str) -> User | None:
    now = _now()
    session = await db.scalar(
        select(UserSession).where(
            UserSession.token_hash == hash_token(token),
            UserSession.expires_at > now,
        )
    )
    if session is None:
        return None

    if now - session.last_seen_at > LAST_SEEN_RESOLUTION:
        session.last_seen_at = now
        await db.commit()

    return session.user


async def delete_session(db: AsyncSession, token: str) -> None:
    await db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(token)))
    await db.commit()


# Keep used and expired login tokens this long: comfortably past the
# one-hour window that per-address rate limiting counts them in.
LOGIN_TOKEN_RETENTION = timedelta(days=1)


async def delete_expired(db: AsyncSession) -> tuple[int, int]:
    """Delete stale login tokens and expired sessions; return how many of each.

    Run daily by `python -m app.cli cleanup`. Expired rows are already
    useless (every lookup checks expiry); this just stops the tables growing.
    """
    now = _now()
    tokens = await db.execute(
        delete(LoginToken).where(LoginToken.created_at < now - LOGIN_TOKEN_RETENTION)
    )
    sessions = await db.execute(delete(UserSession).where(UserSession.expires_at < now))
    await db.commit()
    return tokens.rowcount, sessions.rowcount  # pyright: ignore[reportAttributeAccessIssue]
