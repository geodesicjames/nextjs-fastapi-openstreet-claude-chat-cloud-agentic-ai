"""Places cache, grid cells and upstream call log.

The geometry column is created with spatial_index=False and its GiST index
is added explicitly below, so the migration states exactly what exists.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "place_cells",
        sa.Column("ix", sa.Integer(), nullable=False),
        sa.Column("iy", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ix", "iy", name=op.f("pk_place_cells")),
    )
    op.create_table(
        "places",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("osm_type", sa.String(length=8), nullable=False),
        sa.Column("osm_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=True),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "geom",
            Geometry(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                spatial_index=False,
                from_text="ST_GeomFromEWKT",
                name="geometry",
                nullable=False,
            ),
            nullable=False,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_places")),
        sa.UniqueConstraint("osm_type", "osm_id", name="uq_places_osm"),
    )
    op.create_index("idx_places_geom", "places", ["geom"], postgresql_using="gist")
    op.create_index(op.f("ix_places_category"), "places", ["category"], unique=False)
    op.create_table(
        "upstream_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.String(length=20), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("response_bytes", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upstream_calls")),
    )
    op.create_index(
        "ix_upstream_calls_service_created_at",
        "upstream_calls",
        ["service", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_upstream_calls_service_created_at", table_name="upstream_calls")
    op.drop_table("upstream_calls")
    op.drop_index(op.f("ix_places_category"), table_name="places")
    op.drop_index("idx_places_geom", table_name="places", postgresql_using="gist")
    op.drop_table("places")
    op.drop_table("place_cells")
