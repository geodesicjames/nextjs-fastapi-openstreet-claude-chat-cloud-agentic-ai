"""Agent runs: agent_runs, agent_steps, and llm_usage.agent_run_id.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("task", sa.String(length=40), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error", sa.String(length=40), nullable=True),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_approval', 'done', 'failed', 'cancelled')",
            name=op.f("ck_agent_runs_status"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_agent_runs_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index(
        "ix_agent_runs_user_id_created_at", "agent_runs", ["user_id", "created_at"], unique=False
    )
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name=op.f("fk_agent_steps_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_steps")),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_steps_seq"),
    )
    op.add_column("llm_usage", sa.Column("agent_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_llm_usage_agent_run_id_agent_runs"),
        "llm_usage",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_llm_usage_agent_run_id_agent_runs"), "llm_usage", type_="foreignkey"
    )
    op.drop_column("llm_usage", "agent_run_id")
    op.drop_table("agent_steps")
    op.drop_index("ix_agent_runs_user_id_created_at", table_name="agent_runs")
    op.drop_table("agent_runs")
