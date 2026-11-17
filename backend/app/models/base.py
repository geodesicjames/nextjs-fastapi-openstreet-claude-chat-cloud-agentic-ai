"""Declarative base and shared column helpers.

`Base` is the equivalent of Django's `models.Model`: every table class inherits
from it, and Alembic reads `Base.metadata` to autogenerate migrations.
"""

from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names (e.g. "uq_users_email") so migrations can
# refer to constraints by name. Django names them for you; SQLAlchemy needs
# a convention, otherwise Postgres picks names Alembic cannot predict.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Adds `created_at`, set by the database on insert (like auto_now_add).

    eager_defaults fetches the database-generated value back in the INSERT's
    RETURNING clause. Otherwise, reading `created_at` after a commit would
    trigger a lazy load, which async SQLAlchemy cannot do implicitly.
    """

    __mapper_args__ = {"eager_defaults": True}

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
