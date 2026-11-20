"""Request and response schemas for agent runs (the trip planner)."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import AgentRun, AgentStep, StepKind


class RunIn(BaseModel):
    goal: str = Field(min_length=3)


class FeedbackIn(BaseModel):
    text: str = Field(min_length=1)


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task: str
    goal: str
    # queued | running | awaiting_approval | done | failed | cancelled
    status: str
    error: str | None
    # The checked itinerary (see app/services/trip_planner.py), once there is one.
    plan: dict[str, Any] | None
    model_calls: int
    cost_usd: float
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class StepOut(BaseModel):
    """One step of a run, as people see it.

    `detail` depends on `kind`:
    - goal, nudge, feedback: {text}
    - model: {text, tool_calls: [{name, input}], stop_reason, and token counts}
      (thinking blocks and their signatures are never sent out)
    - tool: {name, input, summary, is_error, places}
    - check: {ok, problems}
    """

    seq: int
    kind: str
    created_at: datetime
    duration_ms: int | None
    cost_usd: float | None
    detail: dict[str, Any]

    @classmethod
    def from_step(cls, step: AgentStep) -> "StepOut":
        return cls(
            seq=step.seq,
            kind=step.kind,
            created_at=step.created_at,
            duration_ms=step.duration_ms,
            cost_usd=float(step.cost_usd) if step.cost_usd is not None else None,
            detail=_detail(step),
        )


def _detail(step: AgentStep) -> dict[str, Any]:
    data = step.data
    if step.kind == StepKind.MODEL:
        blocks = data["content"]
        return {
            "text": "".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
            "tool_calls": [
                {"name": b.get("name"), "input": b.get("input")}
                for b in blocks
                if b.get("type") == "tool_use"
            ],
            "stop_reason": data.get("stop_reason"),
            "input_tokens": data.get("input_tokens"),
            "output_tokens": data.get("output_tokens"),
            "cache_read_input_tokens": data.get("cache_read_input_tokens"),
            "cache_creation_input_tokens": data.get("cache_creation_input_tokens"),
        }
    if step.kind == StepKind.TOOL:
        keys = ("name", "input", "summary", "is_error", "places")
        return {k: data.get(k) for k in keys}
    if step.kind == StepKind.CHECK:
        return {"ok": data["ok"], "problems": data["problems"]}
    return {"text": data.get("text", "")}


class RunDetail(RunOut):
    steps: list[StepOut]

    @classmethod
    def from_run(cls, run: AgentRun, steps: list[AgentStep]) -> "RunDetail":
        return cls(
            **RunOut.model_validate(run).model_dump(),
            steps=[StepOut.from_step(s) for s in steps],
        )


# --- Admin: full traces -------------------------------------------------------------

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class AdminRunOut(RunOut):
    """A run in the admin list: whose it is and which model ran it."""

    user_email: str
    model: str

    @classmethod
    def from_run(cls, run: AgentRun, user_email: str) -> "AdminRunOut":
        return cls(
            **RunOut.model_validate(run).model_dump(), user_email=user_email, model=run.model
        )


class TraceStepOut(BaseModel):
    """One step exactly as stored, which is exactly what the model saw.

    `data` is the stored step (see AgentStep in app/models/agent.py), with
    thinking signatures and encrypted thinking left out: they are opaque
    bytes that only matter when replaying the conversation to the API.
    """

    seq: int
    kind: str
    created_at: datetime
    duration_ms: int | None
    cost_usd: float | None
    data: dict[str, Any]

    @classmethod
    def from_step(cls, step: AgentStep) -> "TraceStepOut":
        data = step.data
        if step.kind == StepKind.MODEL:
            data = {**data, "content": [_readable_block(b) for b in data["content"]]}
        return cls(
            seq=step.seq,
            kind=step.kind,
            created_at=step.created_at,
            duration_ms=step.duration_ms,
            cost_usd=float(step.cost_usd) if step.cost_usd is not None else None,
            data=data,
        )


def _readable_block(block: dict[str, Any]) -> dict[str, Any]:
    if block.get("type") == "redacted_thinking":
        return {"type": "redacted_thinking"}
    return {k: v for k, v in block.items() if k != "signature"}


class RunTrace(AdminRunOut):
    steps: list[TraceStepOut]
    # Summed over the run's model calls.
    tokens: dict[str, int]

    @classmethod
    def from_run_steps(cls, run: AgentRun, user_email: str, steps: list[AgentStep]) -> "RunTrace":
        model_steps = [s.data for s in steps if s.kind == StepKind.MODEL]
        return cls(
            **AdminRunOut.from_run(run, user_email).model_dump(),
            steps=[TraceStepOut.from_step(s) for s in steps],
            tokens={f: sum(d.get(f) or 0 for d in model_steps) for f in TOKEN_FIELDS},
        )
