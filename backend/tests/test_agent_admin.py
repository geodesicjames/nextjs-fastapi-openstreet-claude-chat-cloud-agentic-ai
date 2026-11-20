"""Admin views of agent runs: everyone's runs, and one run's full trace."""

import json
from typing import Any, cast

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AgentRun, User
from app.services import agent
from app.services.agent_worker import drive_run
from app.services.trip_planner import TRIP_PLANNER
from tests.chat_helpers import FakeChatModel
from tests.factories import grant, make_user, signed_in_user
from tests.trip_helpers import add_spots, good_run_script


async def planned_run(db: AsyncSession, email: str = "walker@example.com") -> AgentRun:
    """A run driven to approval by the scripted model (3 model calls)."""
    spots = await add_spots(db)
    user = await make_user(db, email)
    await grant(db, user)
    run = await agent.create_run(db, get_settings(), user, TRIP_PLANNER, "Coffee and a park")
    none = cast(Any, None)  # Overpass and Nominatim: the caches are fresh
    model = FakeChatModel()
    model.replies += good_run_script(spots)
    await drive_run(db, get_settings(), model, run.id, none, none)
    await db.refresh(run)
    return run


async def test_admins_see_everyones_runs(db: AsyncSession, client: AsyncClient) -> None:
    run = await planned_run(db)
    await signed_in_user(db, client, "owner@example.com", is_admin=True)
    runs = (await client.get("/admin/agent/runs")).json()
    assert [(r["id"], r["user_email"], r["status"]) for r in runs] == [
        (str(run.id), "walker@example.com", "awaiting_approval")
    ]
    assert runs[0]["model"] == get_settings().agent_model


async def test_a_trace_shows_every_step_as_the_model_saw_it(
    db: AsyncSession, client: AsyncClient
) -> None:
    run = await planned_run(db)
    await signed_in_user(db, client, "owner@example.com", is_admin=True)
    trace = (await client.get(f"/admin/agent/runs/{run.id}")).json()

    assert [s["kind"] for s in trace["steps"]] == [
        "goal", "model", "tool", "model", "tool", "model", "check",
    ]  # fmt: skip
    model_step = trace["steps"][1]
    assert model_step["data"]["content"][0] == {"type": "thinking", "thinking": ""}
    assert model_step["data"]["stop_reason"] == "tool_use"
    assert model_step["cost_usd"] > 0
    # Tool steps carry the exact output the model was given.
    search = trace["steps"][4]["data"]
    assert search["name"] == "search_places"
    assert "Bean There" in search["output"]
    assert trace["steps"][6]["data"]["ok"] is True
    # Tokens summed over the three model calls (1,000 in and 200 out each).
    assert trace["tokens"]["input_tokens"] == 3_000
    assert trace["tokens"]["output_tokens"] == 600
    # Signatures are opaque replay data: never sent out, even to admins.
    assert "sig-abc" not in json.dumps(trace)


async def test_traces_are_for_admins_only(
    db: AsyncSession, client: AsyncClient, chatter: User
) -> None:
    run = await planned_run(db)
    assert (await client.get("/admin/agent/runs")).status_code == 403
    assert (await client.get(f"/admin/agent/runs/{run.id}")).status_code == 403


async def test_unknown_run_is_404(db: AsyncSession, client: AsyncClient) -> None:
    await signed_in_user(db, client, "owner@example.com", is_admin=True)
    response = await client.get("/admin/agent/runs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
