"""Models for agent runs (the trip planner).

- `AgentRun`: one goal the agent works on ("a half-day walk with coffee and
  a viewpoint"), its status, the plan it settled on, and running totals the
  guardrails check (model calls, cost).
- `AgentStep`: everything that happened in a run, in order: the goal, each
  model call, each tool call, each check of a proposed plan, and each piece
  of human feedback.

The steps are both the trace (what an admin reads to see why the agent did
something) and the run's state: the conversation sent to the model is
rebuilt from them on every call (see app/services/agent.py). So a run
interrupted by a restart can carry on from its last saved step, and nothing
about a run lives only in memory. Steps are append-only, like chat history.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class RunStatus(StrEnum):
    """Where a run is. Transitions (app/services/agent.py):

    queued -> running -> awaiting_approval -> done
                 |               |
                 |               +-> queued (changes requested)
                 +-> failed (a guardrail, the API, or a refusal)
    queued / running / awaiting_approval -> cancelled
    """

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


FINISHED = (RunStatus.DONE, RunStatus.FAILED, RunStatus.CANCELLED)


class StepKind(StrEnum):
    GOAL = "goal"  # the user's goal: the first message
    MODEL = "model"  # one model call: plans, decides what to do next, revises
    TOOL = "tool"  # one tool call the model asked for: acts
    CHECK = "check"  # the code check of a proposed plan
    FEEDBACK = "feedback"  # a person asked for changes to a checked plan
    NUDGE = "nudge"  # a note from the loop itself ("last step: submit now")


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_approval', 'done', 'failed', 'cancelled')",
            name="status",
        ),
        Index("ix_agent_runs_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Which kind of agent (the task), e.g. "trip_planner".
    task: Mapped[str] = mapped_column(String(40))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default=RunStatus.QUEUED)
    # Why it failed: step_limit | cost_limit | time_limit | budget | unavailable
    # | refused | truncated | check_failed | no_access | internal.
    error: Mapped[str | None] = mapped_column(String(40))
    # The last plan that passed the check (the finish tool's input).
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model: Mapped[str] = mapped_column(String(100))
    # Running totals for the guardrails, across every leg of the run.
    model_calls: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    # Updated before every model and tool call while running. A run still
    # "running" with an old heartbeat was interrupted (e.g. by a restart)
    # and may be picked up again.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentStep(TimestampMixin, Base):
    """One thing that happened in a run. `data` depends on `kind`:

    - goal:     {"text"}
    - model:    {"content": [API content blocks, stored exactly], "stop_reason",
                 "input_tokens", "output_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens"}
    - tool:     {"tool_use_id", "name", "input", "output", "is_error", "summary"}
    - check:    {"tool_use_id", "ok", "problems": [str]}
    - feedback: {"tool_use_id", "text"}
    - nudge:    {"text"}
    """

    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_agent_steps_seq"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    seq: Mapped[int]  # 0, 1, 2... order within the run
    kind: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Model steps only.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # Model and tool steps: how long the call took.
    duration_ms: Mapped[int | None]
