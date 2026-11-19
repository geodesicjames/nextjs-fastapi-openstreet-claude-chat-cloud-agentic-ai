"""A hand-written agent loop: plan -> act -> check -> revise, with guardrails.

Chat (app/services/chat.py) answers one message and stops. An agent works
towards a goal over many model calls, and this module is the loop that
drives it. It is written by hand, without an agent framework, so every
decision the loop makes is ordinary code that can be read and tested.

The loop, for one leg of a run (`advance`):

1. Plan / revise: call the model with the goal and everything so far. It
   either asks for tools (searches) or proposes a result by calling the
   task's *finish tool* (e.g. submit_plan).
2. Act: run the tools it asked for, and record each result.
3. Check: a proposed result is checked by code (`AgentTask.check`), not by
   another model call. If the check fails, its problems go back to the
   model as the finish tool's result, and the loop continues (revise).
4. When a proposal passes, the run pauses at `awaiting_approval`: a person
   approves it (done) or asks for changes, which go back to the model the
   same way a failed check does, and a new leg starts.

What a task supplies (`AgentTask`): the system prompt, the tools, the name
of the finish tool, how to run a tool, and how to check a proposal. The trip
planner is one task; the loop itself knows nothing about trips.

State lives in the database, not in memory. Every step (each model call,
tool call, check and piece of feedback) is saved as an `AgentStep` the
moment it happens, and the conversation sent to the model is rebuilt from
those steps before every call. So:
- a run interrupted by a restart carries on from its last saved step;
- the steps are also the trace an admin reads;
- as in chat, history is append-only and replayed byte for byte (each
  step stores the exact text the model was sent), which keeps the prompt
  cache and the model's replayed thinking valid.

Guardrails, checked before every model call:
- the run's model calls (AGENT_MAX_MODEL_CALLS): the last call is told to
  submit; a run that still hasn't is stopped;
- the run's estimated cost (AGENT_MAX_COST_USD);
- wall-clock time per leg (AGENT_MAX_SECONDS);
- the user's chat access and daily budget, the same as chat;
- and every call sets max_tokens. A run stopped by a guardrail ends as
  `failed` with the reason in `error`.

Only one worker may advance a run at a time. Claiming a run is a single
conditional UPDATE (queued -> running), so two workers can't both win, and
every status change is conditional on the current status, so a run
cancelled mid-leg stops at the next step.
"""

import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from anthropic.types import MessageParam, ToolParam
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import AgentRun, AgentStep, RunStatus, StepKind, User
from app.services.chat_access import Denial, get_chat_status, record_usage
from app.services.chat_tools import ToolContext, ToolOutcome
from app.services.llm import ChatModel, ModelReply, ModelUnavailableError

logger = logging.getLogger(__name__)


# --- What a task supplies ------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    problems: list[str] = field(default_factory=list)
    # What to store as the run's plan when ok: e.g. the proposal enriched with
    # place names and times. None stores the finish tool's input as given.
    plan: dict[str, Any] | None = None


# (context, tool name, raw input) -> outcome. Must not raise for bad input.
RunTool = Callable[[ToolContext, str, object], Awaitable[ToolOutcome]]
# (context, run, steps so far, the finish tool's raw input) -> verdict.
CheckProposal = Callable[[ToolContext, AgentRun, list[AgentStep], object], Awaitable[CheckResult]]


@dataclass(frozen=True)
class AgentTask:
    name: str  # stored on the run, e.g. "trip_planner"
    system: str
    tools: list[ToolParam]  # including the finish tool
    finish_tool: str
    run_tool: RunTool
    check: CheckProposal


# --- Text the loop adds to the conversation ------------------------------------------


def _last_call_nudge(task: AgentTask) -> str:
    return f"This is your last step. Call {task.finish_tool} now with your best proposal."


def _keep_going_nudge(task: AgentTask) -> str:
    return (
        f"Keep working until you call {task.finish_tool}. The user can't reply "
        "before then, so don't ask questions: make reasonable assumptions instead."
    )


def _check_failed_result(problems: list[str], task: AgentTask) -> str:
    return json.dumps(
        {
            "accepted": False,
            "problems": problems,
            "next": f"Fix these problems, then call {task.finish_tool} again.",
        },
        ensure_ascii=False,
    )


def _feedback_result(text: str, task: AgentTask) -> str:
    return json.dumps(
        {
            "accepted": False,
            "feedback_from_user": text,
            "next": f"Revise the proposal as the user asks, then call {task.finish_tool} again.",
        },
        ensure_ascii=False,
    )


# --- Rebuilding the conversation from steps -------------------------------------------


def _tool_result(tool_use_id: str, content: str, is_error: bool) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def build_messages(steps: list[AgentStep]) -> list[MessageParam]:
    """The Messages API conversation that the steps add up to.

    Model steps are assistant messages. Everything between two model steps
    (the goal, tool results, check results, feedback, nudges) becomes one
    user message, with tool results first as the API requires.
    """
    messages: list[MessageParam] = []
    pending: list[dict[str, Any]] = []

    def flush() -> None:
        if pending:
            results = [b for b in pending if b["type"] == "tool_result"]
            others = [b for b in pending if b["type"] != "tool_result"]
            messages.append(cast(MessageParam, {"role": "user", "content": results + others}))
            pending.clear()

    for step in steps:
        data = step.data
        if step.kind == StepKind.MODEL:
            flush()
            messages.append(cast(MessageParam, {"role": "assistant", "content": data["content"]}))
        elif step.kind in (StepKind.GOAL, StepKind.NUDGE):
            pending.append({"type": "text", "text": data["text"]})
        elif step.kind == StepKind.TOOL:
            pending.append(_tool_result(data["tool_use_id"], data["output"], data["is_error"]))
        elif step.kind == StepKind.CHECK and not data["ok"]:
            # A passed check has no result yet: approval or feedback answers it.
            pending.append(_tool_result(data["tool_use_id"], data["result"], True))
        elif step.kind == StepKind.FEEDBACK:
            pending.append(_tool_result(data["tool_use_id"], data["result"], False))
    flush()
    return messages


def unanswered_tool_uses(steps: list[AgentStep]) -> list[dict[str, Any]]:
    """Tool calls in the last model step that have no recorded result yet.

    Normally empty when a model call is about to be made; non-empty when a
    leg was interrupted between a model call and its tools, or when a model
    step has just been saved.
    """
    last = next((i for i in range(len(steps) - 1, -1, -1) if steps[i].kind == StepKind.MODEL), None)
    if last is None:
        return []
    answered = {
        s.data["tool_use_id"]
        for s in steps[last + 1 :]
        if s.kind in (StepKind.TOOL, StepKind.FEEDBACK)
        or (s.kind == StepKind.CHECK and not s.data["ok"])
    }
    return [
        block
        for block in steps[last].data["content"]
        if block.get("type") == "tool_use" and block.get("id") not in answered
    ]


# --- Runs and their status ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


async def create_run(
    db: AsyncSession, settings: Settings, user: User, task: AgentTask, goal: str
) -> AgentRun:
    run = AgentRun(
        user_id=user.id,
        task=task.name,
        goal=goal,
        status=RunStatus.QUEUED,
        model=settings.agent_model,
        model_calls=0,
        cost_usd=Decimal(0),
    )
    db.add(run)
    await db.flush()  # assigns run.id
    db.add(AgentStep(run_id=run.id, seq=0, kind=StepKind.GOAL, data={"text": goal}))
    await db.commit()
    return run


async def get_run(db: AsyncSession, user: User, run_id: uuid.UUID) -> AgentRun | None:
    """The user's run, or None (also for other users' ids)."""
    return await db.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user.id)
    )


async def load_steps(db: AsyncSession, run: AgentRun) -> list[AgentStep]:
    return list(
        await db.scalars(
            select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.seq)
        )
    )


async def _transition(
    db: AsyncSession,
    run: AgentRun,
    to: RunStatus,
    from_: tuple[RunStatus, ...],
    **values: Any,
) -> bool:
    """Move the run to `to` if it is currently in one of `from_`. The caller commits.

    A conditional UPDATE, so it is safe against a concurrent change (a
    cancel arriving mid-leg, two workers claiming one run).
    """
    now = _now()
    if to in (RunStatus.DONE, RunStatus.FAILED, RunStatus.CANCELLED):
        values.setdefault("finished_at", now)
    result = await db.execute(
        update(AgentRun)
        .where(AgentRun.id == run.id, AgentRun.status.in_(from_))
        .values(status=to, updated_at=now, **values)
        .returning(AgentRun.id)
        .execution_options(synchronize_session=False)
    )
    changed = result.first() is not None
    await db.refresh(run)
    return changed


async def approve(db: AsyncSession, run: AgentRun) -> bool:
    """Accept the checked proposal. False if the run isn't awaiting approval."""
    changed = await _transition(db, run, RunStatus.DONE, (RunStatus.AWAITING_APPROVAL,))
    await db.commit()
    return changed


async def request_changes(db: AsyncSession, run: AgentRun, text: str, task: AgentTask) -> bool:
    """Send the proposal back with the user's feedback; the run is queued again."""
    if not await _transition(db, run, RunStatus.QUEUED, (RunStatus.AWAITING_APPROVAL,)):
        await db.commit()
        return False
    steps = await load_steps(db, run)
    passed = next(s for s in reversed(steps) if s.kind == StepKind.CHECK and s.data["ok"])
    db.add(
        AgentStep(
            run_id=run.id,
            seq=len(steps),
            kind=StepKind.FEEDBACK,
            data={
                "tool_use_id": passed.data["tool_use_id"],
                "text": text,
                "result": _feedback_result(text, task),
            },
        )
    )
    await db.commit()
    return True


async def cancel(db: AsyncSession, run: AgentRun) -> bool:
    """Stop the run. A leg in progress stops before its next step."""
    changed = await _transition(
        db,
        run,
        RunStatus.CANCELLED,
        (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL),
    )
    await db.commit()
    return changed


async def _claim(db: AsyncSession, settings: Settings, run: AgentRun) -> bool:
    """Take the run for this worker: queued, or running but interrupted."""
    now = _now()
    stale = now - timedelta(seconds=settings.agent_stale_after_seconds)
    result = await db.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run.id,
            or_(
                AgentRun.status == RunStatus.QUEUED,
                and_(AgentRun.status == RunStatus.RUNNING, AgentRun.heartbeat_at < stale),
            ),
        )
        .values(status=RunStatus.RUNNING, heartbeat_at=now, updated_at=now)
        .returning(AgentRun.id)
        .execution_options(synchronize_session=False)
    )
    claimed = result.first() is not None
    await db.commit()
    await db.refresh(run)
    return claimed


async def _heartbeat(db: AsyncSession, run: AgentRun) -> bool:
    """Record that the run is alive; False if it is no longer running (cancelled)."""
    result = await db.execute(
        update(AgentRun)
        .where(AgentRun.id == run.id, AgentRun.status == RunStatus.RUNNING)
        .values(heartbeat_at=_now())
        .returning(AgentRun.id)
        .execution_options(synchronize_session=False)
    )
    alive = result.first() is not None
    await db.commit()
    if not alive:
        await db.refresh(run)
    return alive


# --- One leg of a run -------------------------------------------------------------------


async def advance(
    db: AsyncSession,
    settings: Settings,
    model: ChatModel,
    task: AgentTask,
    run: AgentRun,
    ctx: ToolContext,
) -> AsyncGenerator[AgentStep]:
    """Work on the run until it awaits approval or ends, yielding each new step.

    Does nothing if the run can't be claimed (already running elsewhere,
    awaiting approval, or finished). Never raises for API errors: they end
    the run as failed.
    """
    if not await _claim(db, settings, run):
        return
    started = time.monotonic()
    steps = await load_steps(db, run)

    def add(kind: StepKind, data: dict[str, Any], **extra: Any) -> AgentStep:
        step = AgentStep(run_id=run.id, seq=len(steps), kind=kind, data=data, **extra)
        db.add(step)
        steps.append(step)
        return step

    async def fail(error: str) -> None:
        logger.info("Agent run %s stopped: %s", run.id, error)
        await _transition(db, run, RunStatus.FAILED, (RunStatus.RUNNING,), error=error)
        await db.commit()

    while True:
        if not await _heartbeat(db, run):
            return  # cancelled

        # --- Act and check: answer every tool call of the last model step.
        # The finish tool goes last, so its check sees the other results.
        pending = unanswered_tool_uses(steps)
        pending.sort(key=lambda block: block.get("name") == task.finish_tool)
        for block in pending:
            tool_use_id, name, raw_input = (
                str(block.get("id")),
                str(block.get("name")),
                block.get("input"),
            )
            if name == task.finish_tool:
                verdict = await task.check(ctx, run, steps, raw_input)
                if verdict.ok:
                    step = add(
                        StepKind.CHECK,
                        {"tool_use_id": tool_use_id, "ok": True, "problems": [], "result": None},
                    )
                    plan = verdict.plan
                    if plan is None and isinstance(raw_input, dict):
                        plan = raw_input
                    await _transition(
                        db, run, RunStatus.AWAITING_APPROVAL, (RunStatus.RUNNING,), plan=plan
                    )
                    await db.commit()
                    yield step
                    return
                step = add(
                    StepKind.CHECK,
                    {
                        "tool_use_id": tool_use_id,
                        "ok": False,
                        "problems": verdict.problems,
                        "result": _check_failed_result(verdict.problems, task),
                    },
                )
            else:
                t0 = time.monotonic()
                try:
                    outcome = await task.run_tool(ctx, name, raw_input)
                except Exception:  # a bug or DB failure in a tool must not end the run
                    logger.exception("Agent tool %s failed", name)
                    outcome = ToolOutcome(
                        '{"error": "The tool failed."}', is_error=True, summary="Failed"
                    )
                step = add(
                    StepKind.TOOL,
                    {
                        "tool_use_id": tool_use_id,
                        "name": name,
                        "input": raw_input,
                        "output": outcome.content,
                        "is_error": outcome.is_error,
                        "summary": outcome.summary,
                        "places": outcome.places,
                    },
                    duration_ms=round((time.monotonic() - t0) * 1000),
                )
            await db.commit()
            yield step

        # --- Guardrails, before every model call.
        status = await get_chat_status(db, settings, ctx.user)
        if run.model_calls >= settings.agent_max_model_calls:
            last_check = next((s for s in reversed(steps) if s.kind == StepKind.CHECK), None)
            failed_check = last_check is not None and steps[-1] is last_check
            await fail("check_failed" if failed_check else "step_limit")
            return
        if run.cost_usd >= settings.agent_max_cost_usd:
            await fail("cost_limit")
            return
        if time.monotonic() - started > settings.agent_max_seconds:
            await fail("time_limit")
            return
        if status.denial is not None:
            await fail("budget" if status.denial == Denial.BUDGET_EXHAUSTED else "no_access")
            return
        if run.model_calls == settings.agent_max_model_calls - 1:
            step = add(StepKind.NUDGE, {"text": _last_call_nudge(task), "reason": "last_call"})
            await db.commit()
            yield step

        # --- Plan / revise: one model call.
        t0 = time.monotonic()
        reply: ModelReply | None = None
        try:
            async for item in model.stream(
                model=run.model,
                system=task.system,
                messages=build_messages(steps),
                max_tokens=settings.agent_max_tokens,
                effort=settings.agent_effort,
                tools=task.tools,
            ):
                if isinstance(item, ModelReply):
                    reply = item
        except ModelUnavailableError:
            reply = None
        if reply is None:
            await fail("unavailable")
            return

        usage = await record_usage(
            db,
            user=ctx.user,
            grant=status.grant,
            model=reply.model,
            usage=reply.usage,
            stop_reason=reply.stop_reason,
            agent_run_id=run.id,
            commit=False,
        )
        run.model_calls += 1
        run.cost_usd += usage.cost_usd
        step = add(
            StepKind.MODEL,
            {
                "content": reply.content,
                "stop_reason": reply.stop_reason,
                "input_tokens": reply.usage.input_tokens,
                "output_tokens": reply.usage.output_tokens,
                "cache_read_input_tokens": reply.usage.cache_read_input_tokens,
                "cache_creation_input_tokens": reply.usage.cache_creation_input_tokens,
            },
            cost_usd=usage.cost_usd,
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        yield step

        tool_uses = [b for b in reply.content if b.get("type") == "tool_use"]
        if reply.stop_reason == "refusal":
            await fail("refused")
            return
        if tool_uses and reply.stop_reason == "max_tokens":
            await fail("truncated")  # a cut-off tool call can't be run or replayed
            return
        if not tool_uses:
            # It answered in text instead of acting or proposing.
            step = add(StepKind.NUDGE, {"text": _keep_going_nudge(task), "reason": "keep_going"})
            await db.commit()
            yield step
