"""Models for magic-link login tokens and server-side sessions.

Neither table stores a usable secret. The random token goes to the user (in
an email link or a cookie) and only its SHA-256 hash is stored, the same way
you would treat a password, so a leaked database dump cannot be used to sign
in. Unlike passwords, these tokens are long and random, so a fast hash is
enough: there is nothing to brute-force.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.user import User


class LoginToken(TimestampMixin, Base):
    """A single-use magic-link token.

    Keyed by email rather than user, because the user row is only created
    when the link is first used.
    """

    __tablename__ = "login_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(TimestampMixin, Base):
    """A signed-in browser session (Django's `django_session`, roughly).

    Named UserSession to avoid confusion with SQLAlchemy's database Session.
    Deleting the row signs that browser out immediately.
    """

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # lazy="joined" loads the user in the same query as the session, which
    # matters in async code: a lazy load on attribute access would fail.
    user: Mapped[User] = relationship(lazy="joined")
