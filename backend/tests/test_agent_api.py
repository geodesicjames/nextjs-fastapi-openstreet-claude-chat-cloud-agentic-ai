"""The agent API: start a run, read and stream its steps, approve, revise, cancel.

The background worker is swapped for one that drives the run inline, inside
the request that starts it, on the test's own DB session. So by the time
POST /agent/runs returns, the run has gone as far as it can.
"""

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import get_chat_model
from app.models import Place, RunStatus, User
from app.services import agent, agent_worker
from app.services.agent import AgentTask, CheckResult
from app.services.agent_worker import AgentWorker, drive_run, get_agent_worker
from app.services.trip_planner import TRIP_PLANNER
from tests.chat_helpers import FakeChatModel
from tests.factories import grant, make_user, signed_in_user
from tests.trip_helpers import add_spots, call, good_run_script, plan


@dataclass
class InlineWorker:
    db: AsyncSession
    model: FakeChatModel
    run_now: bool = True
    started: list[uuid.UUID] = field(default_factory=list)

    async def start(self, run_id: uuid.UUID) -> None:
        self.started.append(run_id)
        if self.run_now:
            none = cast(Any, None)  # Overpass and Nominatim: the caches are fresh
            await drive_run(self.db, get_settings(), self.model, run_id, none, none)


@pytest.fixture
def worker(app: FastAPI, db: AsyncSession, fake_model: FakeChatModel) -> InlineWorker:
    w = InlineWorker(db, fake_model)
    app.dependency_overrides[get_agent_worker] = lambda: w
    return w


@pytest.fixture
async def spots(db: AsyncSession) -> dict[str, Place]:
    return await add_spots(db)


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines() if not line.startswith(":"))
        if lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


async def start(client: AsyncClient, goal: str = "Coffee and a park near Leith Walk") -> dict:
    response = await client.post("/agent/runs", json={"goal": goal})
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_run_plans_and_waits_for_approval(
    client: AsyncClient,
    chatter: User,
    worker: InlineWorker,
    fake_model: FakeChatModel,
    spots: dict[str, Place],
) -> None:
    fake_model.replies += good_run_script(spots)
    run = await start(client)
    assert worker.started == [uuid.UUID(run["id"])]

    detail = (await client.get(f"/agent/runs/{run['id']}")).json()
    assert detail["status"] == "awaiting_approval"
    assert detail["task"] == "trip_planner"
    assert [s["name"] for s in detail["plan"]["stops"]] == ["Bean There", "Pilrig Park"]
    kinds = [s["kind"] for s in detail["steps"]]
    assert kinds == ["goal", "model", "tool", "model", "tool", "model", "check"]
    # People see what the model did, never its thinking blocks or signatures.
    first_model = detail["steps"][1]
    assert first_model["detail"]["tool_calls"] == [
        {"name": "find_location", "input": {"query": "Leith Walk"}}
    ]
    assert "sig-abc" not in json.dumps(detail)
    assert first_model["cost_usd"] > 0
    search = detail["steps"][4]["detail"]
    assert search["name"] == "search_places" and len(search["places"]) == 2

    approved = (await client.post(f"/agent/runs/{run['id']}/approve")).json()
    assert approved["status"] == "done"
    again = await client.post(f"/agent/runs/{run['id']}/approve")
    assert again.status_code == 409


async def test_events_stream_steps_then_the_run(
    client: AsyncClient,
    chatter: User,
    worker: InlineWorker,
    fake_model: FakeChatModel,
    spots: dict[str, Place],
) -> None:
    fake_model.replies += good_run_script(spots)
    run = await start(client)

    response = await client.get(f"/agent/runs/{run['id']}/events")
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [e for e, _ in events] == ["step"] * 7 + ["run"]
    assert [d["seq"] for e, d in events if e == "step"] == list(range(7))
    assert events[-1][1]["status"] == "awaiting_approval"

    # Reconnecting with the last seq seen sends only what came after it.
    later = parse_sse((await client.get(f"/agent/runs/{run['id']}/events?after=4")).text)
    assert [d["seq"] for e, d in later if e == "step"] == [5, 6]


async def test_requested_changes_run_again(
    client: AsyncClient,
    chatter: User,
    worker: InlineWorker,
    fake_model: FakeChatModel,
    spots: dict[str, Place],
) -> None:
    fake_model.replies += good_run_script(spots)
    run = await start(client)
    fake_model.replies.append(
        call("submit_plan", plan((spots["park"], 20), (spots["cafe"], 30)), "t4")
    )

    response = await client.post(
        f"/agent/runs/{run['id']}/changes", json={"text": "Park first, please."}
    )
    assert response.status_code == 200, response.text
    detail = (await client.get(f"/agent/runs/{run['id']}")).json()
    assert detail["status"] == "awaiting_approval"
    assert [s["name"] for s in detail["plan"]["stops"]] == ["Pilrig Park", "Bean There"]
    feedback = next(s for s in detail["steps"] if s["kind"] == "feedback")
    assert feedback["detail"] == {"text": "Park first, please."}
    assert len(worker.started) == 2

    # Only a plan awaiting approval can be revised.
    await client.post(f"/agent/runs/{run['id']}/approve")
    late = await client.post(f"/agent/runs/{run['id']}/changes", json={"text": "More"})
    assert late.status_code == 409


async def test_cancel(client: AsyncClient, chatter: User, worker: InlineWorker) -> None:
    worker.run_now = False  # stays queued
    run = await start(client)
    cancelled = (await client.post(f"/agent/runs/{run['id']}/cancel")).json()
    assert cancelled["status"] == "cancelled"
    assert (await client.post(f"/agent/runs/{run['id']}/cancel")).status_code == 409


async def test_one_run_in_progress_at_a_time(
    client: AsyncClient, chatter: User, worker: InlineWorker
) -> None:
    worker.run_now = False
    await start(client)
    response = await client.post("/agent/runs", json={"goal": "Another one"})
    assert response.status_code == 409


async def test_listing_shows_only_my_runs(
    db: AsyncSession, client: AsyncClient, chatter: User, worker: InlineWorker
) -> None:
    worker.run_now = False
    run = await start(client, "Mine")
    other = await make_user(db, "other@example.com")
    await agent.create_run(db, get_settings(), other, TRIP_PLANNER, "Theirs")

    runs = (await client.get("/agent/runs")).json()
    assert [r["goal"] for r in runs] == ["Mine"]
    assert runs[0]["id"] == run["id"]


async def test_other_users_runs_are_not_found(
    db: AsyncSession, client: AsyncClient, chatter: User, worker: InlineWorker
) -> None:
    other = await make_user(db, "other@example.com")
    theirs = await agent.create_run(db, get_settings(), other, TRIP_PLANNER, "Theirs")
    for method, path in [
        ("get", ""),
        ("get", "/events"),
        ("post", "/approve"),
        ("post", "/cancel"),
    ]:
        response = await client.request(method, f"/agent/runs/{theirs.id}{path}")
        assert response.status_code == 404, path
    response = await client.post(f"/agent/runs/{theirs.id}/changes", json={"text": "x"})
    assert response.status_code == 404


async def test_starting_needs_chat_access(
    db: AsyncSession, client: AsyncClient, worker: InlineWorker
) -> None:
    assert (await client.post("/agent/runs", json={"goal": "Coffee"})).status_code == 401
    await signed_in_user(db, client, "nogrant@example.com")
    assert (await client.post("/agent/runs", json={"goal": "Coffee"})).status_code == 403
    assert worker.started == []


async def test_goal_length_is_capped(
    client: AsyncClient, chatter: User, worker: InlineWorker
) -> None:
    too_long = "x" * (get_settings().chat_max_message_chars + 1)
    assert (await client.post("/agent/runs", json={"goal": too_long})).status_code == 422
    assert (await client.post("/agent/runs", json={"goal": "hi"})).status_code == 422


# --- The worker -------------------------------------------------------------------------


async def test_a_crash_in_the_loop_fails_the_run(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, spots: dict[str, Place]
) -> None:
    async def broken_check(*_: Any) -> CheckResult:
        raise RuntimeError("bug in the check")

    broken = AgentTask(**{**TRIP_PLANNER.__dict__, "check": broken_check})
    monkeypatch.setitem(agent_worker.TASKS, "trip_planner", broken)
    user = await make_user(db, "walker@example.com")
    await grant(db, user)
    run = await agent.create_run(db, get_settings(), user, TRIP_PLANNER, "Coffee")
    model = FakeChatModel([call("submit_plan", plan((spots["cafe"], 30)), "t1")])

    none = cast(Any, None)
    await drive_run(db, get_settings(), model, run.id, none, none)
    await db.refresh(run)
    assert run.status == RunStatus.FAILED and run.error == "internal"


async def test_the_sweep_finds_queued_and_interrupted_runs(db: AsyncSession) -> None:
    user = await make_user(db, "walker@example.com")
    settings = get_settings()
    queued = await agent.create_run(db, settings, user, TRIP_PLANNER, "Queued")
    interrupted = await agent.create_run(db, settings, user, TRIP_PLANNER, "Interrupted")
    busy = await agent.create_run(db, settings, user, TRIP_PLANNER, "Busy")
    done = await agent.create_run(db, settings, user, TRIP_PLANNER, "Done")
    now = agent._now()
    stale = now - timedelta(seconds=settings.agent_stale_after_seconds + 60)
    for run, status, heartbeat in [
        (interrupted, RunStatus.RUNNING, stale),
        (busy, RunStatus.RUNNING, now),
        (done, RunStatus.DONE, stale),
    ]:
        run.status, run.heartbeat_at = status, heartbeat
    await db.commit()

    found = set(await agent.find_claimable(db, settings))
    assert {queued.id, interrupted.id} <= found
    assert not {busy.id, done.id} & found


async def test_the_worker_drives_each_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    driven: list[uuid.UUID] = []

    async def fake_drive(self: AgentWorker, run_id: uuid.UUID) -> None:
        driven.append(run_id)

    monkeypatch.setattr(AgentWorker, "_drive", fake_drive)
    worker = AgentWorker(cast(Any, None), lambda *_: None)
    run_id = uuid.uuid4()
    await worker.start(run_id)
    await worker.start(run_id)  # already being driven here: ignored
    await asyncio.gather(*worker.tasks.values())
    await asyncio.sleep(0)  # let the done-callback run
    assert driven == [run_id]
    assert worker.tasks == {}


async def test_the_api_needs_a_configured_model(
    app: FastAPI,
    client: AsyncClient,
    chatter: User,
    worker: InlineWorker,
    use_settings: Callable[..., None],
) -> None:
    del app.dependency_overrides[get_chat_model]  # back to the real one...
    use_settings(anthropic_api_key=None)  # ...with no API key
    response = await client.post("/agent/runs", json={"goal": "Coffee"})
    assert response.status_code == 503
    assert worker.started == []
