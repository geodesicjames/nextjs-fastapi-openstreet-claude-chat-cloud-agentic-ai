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
