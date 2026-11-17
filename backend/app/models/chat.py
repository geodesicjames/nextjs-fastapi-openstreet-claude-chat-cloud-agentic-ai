"""Models for invite-only chat access.

- `ChatGrant`: permission for one user to use chat, with a daily token budget.
- `LlmUsage`: one row per Claude API call. The daily budget is the sum of
  today's rows, and the same ledger feeds cost reporting later.
- `AccessRequest`: someone asking for a grant via the public form.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.user import User


class ChatGrant(TimestampMixin, Base):
    """Permission for one user to use chat.

    Grants are never deleted: revoking sets `revoked_at`, so there is a record
    of who had access and when. A partial unique index allows at most one
    non-revoked grant per user, enforced by Postgres rather than by app code.
    """

    __tablename__ = "chat_grants"
    __table_args__ = (
        Index(
            "uq_chat_grants_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Who approved it. Kept (as NULL) if that admin account is later deleted.
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    # NULL means "use settings.chat_default_daily_tokens", so changing the
    # setting updates every grant that has not been given its own limit.
    daily_token_limit: Mapped[int | None]
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Two foreign keys to users, so tell SQLAlchemy which one this is.
    user: Mapped[User] = relationship(foreign_keys=[user_id], lazy="joined")


class LlmUsage(TimestampMixin, Base):
    """Tokens and estimated cost of a single Claude API call."""

    __tablename__ = "llm_usage"
    __table_args__ = (
        # Serves "sum of this user's tokens since midnight".
        Index("ix_llm_usage_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_grants.id", ondelete="SET NULL")
    )
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    # Numeric, not float: money should not pick up binary rounding errors.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))


class AccessRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class AccessRequest(TimestampMixin, Base):
    """A request for chat access from the public form (endpoints in part 2)."""

    __tablename__ = "access_requests"
    __table_args__ = (
        # Stored as plain text plus a CHECK constraint rather than a Postgres
        # ENUM type, which is awkward to change in later migrations.
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied')",
            name="status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), index=True)
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(20), default=AccessRequestStatus.PENDING, server_default="pending"
    )
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
