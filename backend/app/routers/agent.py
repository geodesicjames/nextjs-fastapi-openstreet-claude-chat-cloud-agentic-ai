"""Agent routes: start a trip-planning run, watch it, approve or revise it.

The run itself is driven in the background by the agent worker
(app/services/agent_worker.py), so these routes return straight away:

- POST /agent/runs                      start a run (queued, then running)
- GET  /agent/runs                      the user's recent runs
- GET  /agent/runs/{id}                 a run with all its steps
- GET  /agent/runs/{id}/events?after=N  stream new steps as they happen (SSE)
- POST /agent/runs/{id}/approve         accept the checked plan
- POST /agent/runs/{id}/changes         send it back with feedback (runs again)
- POST /agent/runs/{id}/cancel          stop it

Routes that make model calls (start, changes) depend on `ChatAccess`, like
chat: sign-in, an active grant and budget left. The rest only need the
run's owner to be signed in; other users' runs are 404s.

The events stream reads saved steps from the database, so it can be
dropped and reopened at any time without affecting the run: the client
passes the last `seq` it saw as `after`. Event types: `step` (a StepOut)
and, once the run stops moving (awaiting approval, done, failed or
cancelled), one `run` (a RunOut) before the stream closes.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import ChatAccess, CurrentUser, DbSession, StreamingDbSession, get_chat_model
from app.models import AgentRun, RunStatus, User
from app.schemas.agent import FeedbackIn, RunDetail, RunIn, RunOut, StepOut
from app.services import agent
from app.services.agent_worker import Worker, get_agent_worker
from app.services.llm import ChatModel
from app.services.trip_planner import TRIP_PLANNER

router = APIRouter(prefix="/agent", tags=["agent"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
WorkerDep = Annotated[Worker, Depends(get_agent_worker)]
# Depending on the model makes these routes answer 503 when no API key is
# configured, instead of queueing a run that can't make progress.
ModelDep = Annotated[ChatModel, Depends(get_chat_model)]

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)
RECENT_RUNS = 20
POLL_SECONDS = 0.5
KEEPALIVE_SECONDS = 15
# Streams end after this long; the client reconnects with `after`.
STREAM_SECONDS = 600


async def _own_run(db: AsyncSession, user: User, run_id: uuid.UUID) -> AgentRun:
    run = await agent.get_run(db, user, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    return run


def _check_length(text: str, settings: Settings) -> None:
    if len(text) > settings.chat_max_message_chars:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Limited to {settings.chat_max_message_chars} characters.",
        )


@router.post("/runs", status_code=status.HTTP_201_CREATED, response_model=RunOut)
async def start_run(
    body: RunIn,
    db: DbSession,
    access: ChatAccess,
    settings: SettingsDep,
    worker: WorkerDep,
    _model: ModelDep,
) -> RunOut:
    _check_length(body.goal, settings)
    active = await db.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.user_id == access.user.id, AgentRun.status.in_(ACTIVE))
    )
    if active:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "You already have a plan in progress. Wait or cancel it."
        )
    run = await agent.create_run(db, settings, access.user, TRIP_PLANNER, body.goal.strip())
    await worker.start(run.id)
    await db.refresh(run)
    return RunOut.model_validate(run)


@router.get("/runs", response_model=list[RunOut])
async def list_runs(db: DbSession, user: CurrentUser) -> list[RunOut]:
    runs = await db.scalars(
        select(AgentRun)
        .where(AgentRun.user_id == user.id)
        .order_by(AgentRun.created_at.desc())
        .limit(RECENT_RUNS)
    )
    return [RunOut.model_validate(r) for r in runs]


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID, db: DbSession, user: CurrentUser) -> RunDetail:
    run = await _own_run(db, user, run_id)
    return RunDetail.from_run(run, await agent.load_steps(db, run))


def _sse(event: str, payload: BaseModel) -> str:
    return f"event: {event}\ndata: {payload.model_dump_json()}\n\n"


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: uuid.UUID,
    request: Request,
    db: StreamingDbSession,
    user: CurrentUser,
    after: Annotated[int, Query(ge=-1)] = -1,
) -> StreamingResponse:
    """Stream the run's steps after `after`, then its status once it stops moving."""
    run = await _own_run(db, user, run_id)

    async def events() -> AsyncIterator[str]:
        last = after
        started = last_sent = time.monotonic()
        while True:
            # Status first, then steps: a step is always saved before the
            # status change it leads to, so once a stopped status is seen,
            # every step it depends on is visible too.
            await db.refresh(run)
            steps = await agent.load_steps(db, run, after=last)
            # End the read transaction so the next poll sees new commits
            # (and the connection isn't left "idle in transaction").
            await db.commit()
            for step in steps:
                yield _sse("step", StepOut.from_step(step))
                last = step.seq
                last_sent = time.monotonic()
            if run.status not in ACTIVE:
                yield _sse("run", RunOut.model_validate(run))
                return
            now = time.monotonic()
            if now - started > STREAM_SECONDS or await request.is_disconnected():
                return
            if now - last_sent > KEEPALIVE_SECONDS:
                yield ": keep-alive\n\n"  # an SSE comment: keeps proxies from timing out
                last_sent = now
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/approve", response_model=RunOut)
async def approve_run(run_id: uuid.UUID, db: DbSession, user: CurrentUser) -> RunOut:
    run = await _own_run(db, user, run_id)
    if not await agent.approve(db, run):
        raise HTTPException(status.HTTP_409_CONFLICT, "This plan isn't waiting for approval.")
    return RunOut.model_validate(run)


@router.post("/runs/{run_id}/changes", response_model=RunOut)
async def request_changes(
    run_id: uuid.UUID,
    body: FeedbackIn,
    db: DbSession,
    access: ChatAccess,
    settings: SettingsDep,
    worker: WorkerDep,
    _model: ModelDep,
) -> RunOut:
    _check_length(body.text, settings)
    run = await _own_run(db, access.user, run_id)
    if not await agent.request_changes(db, run, body.text.strip(), TRIP_PLANNER):
        raise HTTPException(status.HTTP_409_CONFLICT, "This plan isn't waiting for approval.")
    await worker.start(run.id)
    await db.refresh(run)
    return RunOut.model_validate(run)


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: uuid.UUID, db: DbSession, user: CurrentUser) -> RunOut:
    run = await _own_run(db, user, run_id)
    if not await agent.cancel(db, run):
        raise HTTPException(status.HTTP_409_CONFLICT, "This run has already finished.")
    return RunOut.model_validate(run)
