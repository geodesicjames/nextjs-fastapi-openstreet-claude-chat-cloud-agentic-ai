"""SQLAlchemy models.

`Base` is the equivalent of Django's `models.Model`: every table class inherits
from it, and Alembic reads `Base.metadata` to autogenerate migrations. Import
each model module here so Alembic sees all tables.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
