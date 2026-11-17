"""Enable the PostGIS extension.

The postgis/postgis Docker image already enables PostGIS, but managed Postgres
(e.g. AWS RDS) does not, so the migration makes the schema reproducible
anywhere. IF NOT EXISTS keeps it safe on databases that already have it.

Revision ID: 0001
Revises:
Create Date: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")


def downgrade() -> None:
    # Deliberately a no-op: dropping PostGIS would destroy every spatial
    # column in the database, and on Docker the image owns the extension.
    pass
