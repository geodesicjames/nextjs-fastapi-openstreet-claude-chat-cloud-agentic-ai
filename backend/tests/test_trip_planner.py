"""The trip planner task: the itinerary check, and a full run with the agent loop.

Places are real rows in PostGIS (the check looks stops up there); the area's
cells and geocoding answer are cached up front so no test calls Overpass or
Nominatim. The model is the scripted FakeChatModel.
"""

import json
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    AgentRun,
    AgentStep,
    Place,
    RunStatus,
    StepKind,
    User,
)
from app.services import agent
from app.services.chat_tools import ToolContext, distance_m
from app.services.trip_planner import (
    DETOUR_FACTOR,
    TRIP_PLANNER,
    check_plan,
    run_trip_tool,
    walk_minutes,
)
from tests.chat_helpers import FakeChatModel
from tests.factories import grant, make_user
from tests.trip_helpers import LEITH, SPOTS, add_spots, call, plan


@pytest.fixture
async def spots(db: AsyncSession) -> dict[str, Place]:
    return await add_spots(db)


@pytest.fixture
async def user(db: AsyncSession) -> User:
    user = await make_user(db, "walker@example.com")
    await grant(db, user)
    return user


def ctx(db: AsyncSession, user: User) -> ToolContext:
    return ToolContext(
        db=db,
        settings=get_settings(),
        user=user,
        overpass=cast(Any, None),  # the cache is fresh: never called
        nominatim=cast(Any, None),
    )


# --- Steps as the loop would have recorded them -----------------------------------------


def located(point: dict[str, Any]) -> AgentStep:
    output = {"matches": [{"name": point["name"], "lat": point["lat"], "lon": point["lon"]}]}
    return AgentStep(
        kind=StepKind.TOOL,
        data={
            "name": "find_location",
            "output": json.dumps(output),
            "is_error": False,
            "places": [],
        },
    )


def found(*places: Place) -> AgentStep:
    listed = [
        {"place_id": str(p.id), "name": p.name, "category": p.category, **_coords(p)}
        for p in places
    ]
    return AgentStep(
        kind=StepKind.TOOL,
        data={"name": "search_places", "output": "{}", "is_error": False, "places": listed},
    )


def _coords(place: Place) -> dict[str, float]:
    spot = next(s for s in SPOTS.values() if s["name"] == place.name)
    return {"lat": spot["lat"], "lon": spot["lon"]}


async def check(db: AsyncSession, user: User, steps: list[AgentStep], proposal: object):
    return await check_plan(ctx(db, user), cast(AgentRun, None), steps, proposal)


# --- The check ---------------------------------------------------------------------------


def test_walking_estimate() -> None:
    # 800 m in a straight line -> 1040 m of streets -> 13 minutes at 80 m/min.
    assert walk_minutes(800) == 13
    assert walk_minutes(0) == 0


async def test_a_good_plan_passes_and_is_filled_in(
    db: AsyncSession, user: User, spots: dict[str, Place]
) -> None:
    steps = [located(LEITH), found(spots["cafe"], spots["park"])]
    proposal = plan((spots["cafe"], 30), (spots["park"], 20), start_time="10:00", end_by="11:00")
    result = await check(db, user, steps, proposal)

    assert result.ok, result.problems
    assert result.plan is not None
    cafe, park = result.plan["stops"]
    # Names and categories come from the database.
    assert (cafe["name"], cafe["category"]) == ("Bean There", "cafe")
    first_leg = distance_m(LEITH["lat"], LEITH["lon"], SPOTS["cafe"]["lat"], SPOTS["cafe"]["lon"])
    assert cafe["walk_m"] == round(first_leg * DETOUR_FACTOR)
    assert cafe["walk_minutes"] == walk_minutes(first_leg)
    # The timetable adds walks and visits up.
    assert cafe["arrive"] == f"10:{cafe['walk_minutes']:02d}"
    assert park["arrive"] == f"10:{cafe['walk_minutes'] + 30 + park['walk_minutes']:02d}"
    back = result.plan["return"]
    assert result.plan["total_minutes"] == (
        cafe["walk_minutes"] + 30 + park["walk_minutes"] + 20 + back["walk_minutes"]
    )
    assert "estimates" in result.plan["note"]


async def test_stops_must_come_from_this_runs_searches(
    db: AsyncSession, user: User, spots: dict[str, Place]
) -> None:
    # The park exists, but no search in this run returned it.
    steps = [located(LEITH), found(spots["cafe"])]
    result = await check(db, user, steps, plan((spots["cafe"], 30), (spots["park"], 20)))
    assert not result.ok
    assert any("wasn't returned by search_places" in p for p in result.problems)


async def test_requested_categories_must_be_covered(
    db: AsyncSession, user: User, spots: dict[str, Place]
) -> None:
    steps = [located(LEITH), found(spots["cafe"])]
    proposal = plan((spots["cafe"], 30), categories=["cafe", "viewpoint"])
    result = await check(db, user, steps, proposal)
    assert result.problems == ["categories: no stop is a viewpoint. Add one, or search wider."]


async def test_no_repeated_stops(db: AsyncSession, user: User, spots: dict[str, Place]) -> None:
    steps = [located(LEITH), found(spots["cafe"], spots["park"])]
    result = await check(
        db, user, steps, plan((spots["cafe"], 30), (spots["park"], 20), (spots["cafe"], 10))
    )
    assert result.problems == ["stop 3: that place is already an earlier stop."]


@pytest.mark.parametrize(
    ("limits", "problem"),
    [
        ({"max_minutes": 30}, "over the 30 asked for"),
        ({"start_time": "10:00", "end_by": "10:30"}, "after end_by (10:30)"),
        ({"end_by": "12:00"}, "start_time: needed"),
        ({"start_time": "23:30"}, "past midnight"),
    ],
)
async def test_time_limits(
    db: AsyncSession, user: User, spots: dict[str, Place], limits: dict[str, Any], problem: str
) -> None:
    steps = [located(LEITH), found(spots["cafe"], spots["park"])]
    result = await check(db, user, steps, plan((spots["cafe"], 30), (spots["park"], 20), **limits))
    assert not result.ok
    assert any(problem in p for p in result.problems), result.problems


async def test_long_walks_are_refused(
    db: AsyncSession, user: User, spots: dict[str, Place]
) -> None:
    steps = [located(LEITH), found(spots["far"])]
    result = await check(
        db, user, steps, plan((spots["far"], 60), categories=["restaurant"], return_to_start=False)
    )
    assert not result.ok
    assert "walk from Leith Walk to Far Feast is about 7.2 km" in result.problems[0]


async def test_the_start_must_come_from_a_tool(
    db: AsyncSession, user: User, spots: dict[str, Place]
) -> None:
    # A start ~1 km south of anything a tool returned: the model made it up.
    made_up = {"name": "The Meadows", "lat": 55.9500, "lon": -3.1800}
    steps = [located(LEITH), found(spots["cafe"], spots["park"])]
    proposal = plan((spots["cafe"], 30), (spots["park"], 20), start=made_up)
    result = await check(db, user, steps, proposal)
    assert result.problems == [
        "start: use coordinates returned by find_location or search_places, not your own estimate."
    ]


async def test_malformed_proposals_list_every_problem(
    db: AsyncSession, user: User, spots: dict[str, Place]
) -> None:
    proposal = plan((spots["cafe"], 2), start_time="2pm")
    result = await check(db, user, [located(LEITH), found(spots["cafe"])], proposal)
    assert not result.ok
    assert any(p.startswith("start_time:") for p in result.problems)
    assert any(p.startswith("stops.0.minutes:") for p in result.problems)


async def test_the_planner_cannot_save_places(db: AsyncSession, user: User) -> None:
    outcome = await run_trip_tool(ctx(db, user), "save_place", {"place_id": "x", "note": ""})
    assert outcome.is_error and "Unknown tool: save_place" in outcome.content


# --- A whole run ---------------------------------------------------------------------------


async def test_a_run_finds_places_revises_and_waits_for_approval(
    db: AsyncSession, user: User, spots: dict[str, Place]
) -> None:
    search = {
        "lat": LEITH["lat"],
        "lon": LEITH["lon"],
        "radius_m": 800,
        "categories": ["cafe", "park"],
        "name": None,
    }
    model = FakeChatModel(
        [
            call("find_location", {"query": "Leith Walk"}, "t1"),
            call("search_places", search, "t2"),
            # Forgets the park the user asked for...
            call("submit_plan", plan((spots["cafe"], 30)), "t3"),
            # ...and fixes it after the check says so.
            call("submit_plan", plan((spots["cafe"], 30), (spots["park"], 20)), "t4"),
        ]
    )
    settings = get_settings()
    run = await agent.create_run(db, settings, user, TRIP_PLANNER, "Coffee and a park, Leith Walk")
    steps = [s async for s in agent.advance(db, settings, model, TRIP_PLANNER, run, ctx(db, user))]

    assert [s.kind for s in steps] == [
        "model", "tool", "model", "tool", "model", "check", "model", "check",
    ]  # fmt: skip
    assert steps[5].data["problems"] == ["categories: no stop is a park. Add one, or search wider."]
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.plan is not None
    assert [s["name"] for s in run.plan["stops"]] == ["Bean There", "Pilrig Park"]

    # The planner sees the trip tools and the system prompt, and nothing else.
    sent = model.calls[0]
    assert [t["name"] for t in sent["tools"]] == [
        "find_location", "search_places", "get_place_details", "submit_plan",
    ]  # fmt: skip
    assert sent["effort"] == settings.agent_effort
