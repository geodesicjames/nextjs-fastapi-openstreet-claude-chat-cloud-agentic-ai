"""Drives agent runs in the background, inside the API process.

A run takes a minute or two of model and tool calls, so it can't live
inside the HTTP request that starts it: closing the tab would stop it. The
request queues the run and asks the worker to start it; the worker drives
it with its own DB session as an asyncio task, and the browser watches its
progress through a separate, reconnectable stream of steps
(GET /agent/runs/{id}/events).

The nearest Django equivalent is a Celery worker, but nothing here needs a
broker: the queue is the `agent_runs` table itself. A sweeper checks it
every minute for runs nobody is driving (queued ones, and running ones
whose heartbeat went stale because the process restarted mid-run) and
starts them. Claiming a run is a conditional UPDATE (app/services/agent.py),
so starting a run twice, here or in another process, is harmless: only one
worker gets it.

On shutdown the tasks are cancelled. Their runs stay "running" until the
heartbeat goes stale, then the sweeper of the restarted process resumes
them from their last saved step.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Protocol

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import AgentRun, User
from app.services import agent
from app.services.agent import AgentTask
from app.services.chat_tools import ToolContext
from app.services.geocoding import NominatimClient
from app.services.llm import ChatModel
from app.services.overpass import OverpassClient
from app.services.trip_planner import TRIP_PLANNER

logger = logging.getLogger(__name__)

# Agent tasks by the name stored on each run.
TASKS: dict[str, AgentTask] = {TRIP_PLANNER.name: TRIP_PLANNER}

SWEEP_SECONDS = 60


async def drive_run(
    db: AsyncSession,
    settings: Settings,
    model: ChatModel | None,
    run_id: uuid.UUID,
    overpass: OverpassClient,
    nominatim: NominatimClient,
) -> None:
    """Advance one run until it pauses or ends. Never raises."""
    run = await db.get(AgentRun, run_id)
    if run is None:
        return
    try:
        user = await db.get(User, run.user_id)
        task = TASKS.get(run.task)
        if user is None or task is None or model is None:
            raise RuntimeError(f"can't drive run {run_id}: user, task or model missing")
        ctx = ToolContext(
            db=db, settings=settings, user=user, overpass=overpass, nominatim=nominatim
        )
        async for _step in agent.advance(db, settings, model, task, run, ctx):
            pass  # each step is already saved; watchers read them from the DB
    except asyncio.CancelledError:
        raise  # shutdown: the run is resumed after restart
    except Exception:
        logger.exception("Agent run %s crashed", run_id)
        # Rolling back expires every loaded object, so load the run again
        # (async sessions can't lazy-load an expired attribute).
        await db.rollback()
        run = await db.get(AgentRun, run_id)
        if run is not None:
            await agent.mark_failed(db, run, "internal")


class Worker(Protocol):
    async def start(self, run_id: uuid.UUID) -> None:
        """Make sure the run is being driven. Returns without waiting for it."""
        ...


class AgentWorker:
    def __init__(
        self,
        app: FastAPI,
        model_for: Callable[[FastAPI, Settings], ChatModel | None],
        session_factory: Callable[[], AsyncSession] = SessionLocal,
    ) -> None:
        self.app = app
        self.model_for = model_for
        self.session_factory = session_factory
        self.tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def start(self, run_id: uuid.UUID) -> None:
        if run_id in self.tasks:
            return  # already being driven by this process
        task = asyncio.create_task(self._drive(run_id), name=f"agent-run-{run_id}")
        self.tasks[run_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(run_id, None))

    async def _drive(self, run_id: uuid.UUID) -> None:
        settings = get_settings()
        state = self.app.state
        async with self.session_factory() as db:
            await drive_run(
                db,
                settings,
                self.model_for(self.app, settings),
                run_id,
                state.overpass,
                state.nominatim,
            )

    async def sweep(self) -> None:
        """Start every run that nobody is driving."""
        async with self.session_factory() as db:
            run_ids = await agent.find_claimable(db, get_settings())
        for run_id in run_ids:
            await self.start(run_id)

    async def run_sweeper(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception:
                logger.exception("Agent sweep failed")
            await asyncio.sleep(SWEEP_SECONDS)

    async def stop(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def get_agent_worker(request: Request) -> Worker:
    """The app's worker, as a dependency (tests swap in one that runs inline)."""
    return request.app.state.agent_worker
