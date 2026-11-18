"""Chat core: conversations, messages, USD budgets, richer usage log.

Grant budgets switch from daily tokens to daily USD. Existing per-grant token
limits can't be converted meaningfully, so they are dropped and every grant
starts on the CHAT_DAILY_BUDGET_USD default (admins can set a per-grant
amount again). Downgrading likewise resets grants to the token default.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_conversations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(op.f("ix_conversations_user_id"), "conversations", ["user_id"], unique=False)
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_chat_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_chat_messages_seq"),
    )
    op.add_column(
        "chat_grants",
        sa.Column("daily_budget_usd", sa.Numeric(precision=10, scale=2), nullable=True),
    )
    op.drop_column("chat_grants", "daily_token_limit")
    op.add_column("llm_usage", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.add_column(
        "llm_usage",
        sa.Column("cache_creation_input_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "llm_usage",
        sa.Column("cache_read_input_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("llm_usage", sa.Column("stop_reason", sa.String(length=30), nullable=True))
    op.create_foreign_key(
        op.f("fk_llm_usage_conversation_id_conversations"),
        "llm_usage",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_llm_usage_conversation_id_conversations"), "llm_usage", type_="foreignkey"
    )
    op.drop_column("llm_usage", "stop_reason")
    op.drop_column("llm_usage", "cache_read_input_tokens")
    op.drop_column("llm_usage", "cache_creation_input_tokens")
    op.drop_column("llm_usage", "conversation_id")
    op.add_column(
        "chat_grants",
        sa.Column("daily_token_limit", sa.INTEGER(), autoincrement=False, nullable=True),
    )
    op.drop_column("chat_grants", "daily_budget_usd")
    op.drop_table("chat_messages")
    op.drop_index(op.f("ix_conversations_user_id"), table_name="conversations")
    op.drop_table("conversations")
