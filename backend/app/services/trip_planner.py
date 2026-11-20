"""The trip planner: an agent task (see app/services/agent.py).

The user describes a trip ("a half-day walk from Leith Walk: coffee, a park,
a viewpoint, back by 2pm"). The agent finds the area, searches for places,
and proposes an itinerary with `submit_plan`. The proposal is checked here,
in code, and any problems go back to the model to fix:

- every stop is a place a search in this run returned (no invented places),
  and the starting point came from a location lookup or a search;
- every category the user asked for is covered (by the place's real
  category in the database, not the model's description of it);
- no stop appears twice, and no walk between stops is unreasonably long;
- the trip fits the user's limits: a total length, and an end time (which
  needs a start time to check against).

A plan that passes is stored enriched: names, categories and coordinates
from the database, walking distance and time for every leg, and clock times
when there's a start time. That is what the user approves and the map shows.

Walking times are estimates: straight-line distance times a detour factor,
at an average walking pace. Real routing (OSRM/Valhalla, issue #16) could
replace `walk_minutes` later without changing the agent or the check.
"""

import json
import math
import re
import uuid
from datetime import time
from typing import Any, Literal

from anthropic.types import ToolParam
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import ErrorDetails

from app.models import AgentRun, AgentStep, StepKind
from app.services import places
from app.services.agent import AgentTask, CheckResult
from app.services.categories import CATEGORIES
from app.services.chat_tools import TOOLS, ToolContext, ToolOutcome, distance_m, run_tool

# Straight lines underestimate walks: streets and paths add roughly 20-40%.
DETOUR_FACTOR = 1.3
WALK_METRES_PER_MINUTE = 80  # 4.8 km/h, an easy pace
# A single walk longer than this probably isn't what anyone wants.
MAX_LEG_M = 4_000
# The start must be this close to something a tool returned.
START_TOLERANCE_M = 150
MAX_STOPS = 8
ESTIMATE_NOTE = (
    "Walking times are estimates from straight-line distance "
    f"(x{DETOUR_FACTOR} for streets, at {WALK_METRES_PER_MINUTE * 60 / 1000:g} km/h)."
)

Category = Literal["cafe", "restaurant", "park", "drinking_water", "toilets", "viewpoint"]

SYSTEM_PROMPT = """\
You plan short trips on foot inside "Places", an app with an \
OpenStreetMap-based map of cafés, restaurants, parks, toilets, drinking \
water and viewpoints. The user gives you a goal; you work out a plan with \
your tools and submit it with submit_plan. The user can't answer questions \
while you work: make reasonable assumptions and mention them in the summary.

How to work:
1. Call find_location for the starting point or area the user names.
2. Call search_places around it for each kind of place the user wants \
(radius up to 2000 m; start around 800 m and widen if nothing suits).
3. Choose stops only from search_places results, and order them so the \
walk is short: avoid zig-zagging back and forth.
4. If the user mentions times, check opening_hours with get_place_details \
for the stops where it matters, and say in each stop's "why" if hours are \
missing or don't fit.
5. Call submit_plan. Its result tells you if the plan breaks a rule (for \
example a missing category, or a plan too long for the time available); \
fix exactly those problems and submit again.

Rules for submit_plan:
- categories: the kinds of place the user explicitly asked for, using the \
app's category names. Every one must be covered by at least one stop.
- start: the point from find_location (or a place from search_places) \
where the walk begins.
- start_time / end_by: 24-hour "HH:MM" if the user gave them, else null. \
If the user gives an end time but no start time, assume a sensible start \
and say so in the summary.
- max_minutes: the total length the user asked for (e.g. 120 for "two \
hours", 240 for "half a day"), else null.
- minutes per stop: a realistic visit (e.g. 30-45 for a café, 60+ for a \
meal, 20-40 for a park, 10-15 for a viewpoint).
- why: one short sentence from the tool results only (name, category, \
distance, tags). Never invent details, impressions or opening hours.
- summary: one or two plain sentences describing the trip and any \
assumptions you made.
Walking times are calculated for you; don't estimate them yourself."""

SUBMIT_PLAN: ToolParam = {
    "name": "submit_plan",
    "description": (
        "Submit the itinerary for checking. If it breaks a rule, the result lists "
        "the problems: fix them and call this again. If it passes, the user reviews "
        "it and may ask for changes."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "A short title, up to 80 characters."},
            "summary": {
                "type": "string",
                "description": "One or two sentences, including assumptions. Up to 400 characters.",
            },
            "categories": {
                "type": "array",
                "items": {"type": "string", "enum": list(CATEGORIES)},
                "description": "The kinds of place the user asked for.",
            },
            "start": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "lat": {"type": "number"},
                    "lon": {"type": "number"},
                },
                "required": ["name", "lat", "lon"],
                "additionalProperties": False,
            },
            "return_to_start": {
                "type": "boolean",
                "description": "Whether the walk ends back at the start.",
            },
            "start_time": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "24-hour HH:MM, or null.",
            },
            "end_by": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "24-hour HH:MM the user must be done by, or null.",
            },
            "max_minutes": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "description": "Longest total trip the user wants, in minutes, or null.",
            },
            "stops": {
                "type": "array",
                "description": f"The stops in visiting order, 1 to {MAX_STOPS}.",
                "items": {
                    "type": "object",
                    "properties": {
                        "place_id": {
                            "type": "string",
                            "format": "uuid",
                            "description": "A place_id from search_places.",
                        },
                        "minutes": {
                            "type": "integer",
                            "description": "Time to spend there, 5 to 240.",
                        },
                        "why": {
                            "type": "string",
                            "description": "One sentence from tool results only, up to 200 chars.",
                        },
                    },
                    "required": ["place_id", "minutes", "why"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "title",
            "summary",
            "categories",
            "start",
            "return_to_start",
            "start_time",
            "end_by",
            "max_minutes",
            "stops",
        ],
        "additionalProperties": False,
    },
}

# The chat's read-only tools plus submit_plan (no save_place: a plan isn't
# saved anywhere until the user approves it).
TOOL_NAMES = ("find_location", "search_places", "get_place_details")
TRIP_TOOLS: list[ToolParam] = [t for t in TOOLS if t["name"] in TOOL_NAMES] + [SUBMIT_PLAN]


# --- Validating a proposal ------------------------------------------------------------

HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class StartIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class StopIn(BaseModel):
    place_id: uuid.UUID
    minutes: int = Field(ge=5, le=240)
    why: str = Field(max_length=200)


class PlanIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(max_length=400)
    categories: list[Category] = Field(min_length=1)
    start: StartIn
    return_to_start: bool
    start_time: str | None = Field(default=None, pattern=HHMM.pattern)
    end_by: str | None = Field(default=None, pattern=HHMM.pattern)
    max_minutes: int | None = Field(default=None, ge=15, le=12 * 60)
    stops: list[StopIn] = Field(min_length=1, max_length=MAX_STOPS)


def walk_minutes(metres: float) -> int:
    """Estimated walking time for a straight-line distance, rounded up."""
    return math.ceil(metres * DETOUR_FACTOR / WALK_METRES_PER_MINUTE)


def _minutes(hhmm: str) -> int:
    t = time.fromisoformat(hhmm)
    return t.hour * 60 + t.minute


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _what_tools_returned(steps: list[AgentStep]) -> tuple[set[str], list[tuple[float, float]]]:
    """Place ids, and every point (places and looked-up locations), from this run's tools."""
    place_ids: set[str] = set()
    points: list[tuple[float, float]] = []
    for step in steps:
        if step.kind != StepKind.TOOL or step.data.get("is_error"):
            continue
        for place in step.data.get("places") or []:
            place_ids.add(str(place["place_id"]))
            points.append((place["lat"], place["lon"]))
        if step.data.get("name") == "find_location":
            try:
                matches = json.loads(step.data["output"]).get("matches", [])
            except (ValueError, AttributeError):
                matches = []
            points += [(m["lat"], m["lon"]) for m in matches]
    return place_ids, points


def _schema_problem(error: ErrorDetails) -> str:
    """One validation error, worded for the model (and for people reading the trace)."""
    where = ".".join(map(str, error["loc"])) or "input"
    if error["type"] == "string_pattern_mismatch":  # the HH:MM times
        # Show what was sent: models sometimes wrap the time in extra quotes.
        return f"{where}: use 24-hour HH:MM, like 14:30 (got {error['input']!r})."
    return f"{where}: {error['msg']}"


async def check_plan(
    ctx: ToolContext, run: AgentRun, steps: list[AgentStep], proposal: object
) -> CheckResult:
    try:
        plan = PlanIn.model_validate(proposal)
    except ValidationError as error:
        return CheckResult(ok=False, problems=[_schema_problem(e) for e in error.errors()])

    problems: list[str] = []
    seen_ids, seen_points = _what_tools_returned(steps)

    if not any(
        distance_m(plan.start.lat, plan.start.lon, lat, lon) <= START_TOLERANCE_M
        for lat, lon in seen_points
    ):
        problems.append(
            "start: use coordinates returned by find_location or search_places, "
            "not your own estimate."
        )

    # Look every stop up in the database: names, categories and positions
    # come from there, never from the model.
    stops: list[dict[str, Any]] = []
    for i, stop in enumerate(plan.stops, start=1):
        place_id = str(stop.place_id)
        if place_id not in seen_ids:
            problems.append(f"stop {i}: {place_id} wasn't returned by search_places in this plan.")
            continue
        if any(s["place_id"] == place_id for s in stops):
            problems.append(f"stop {i}: that place is already an earlier stop.")
            continue
        hit = await places.get_place(ctx.db, stop.place_id)
        if hit is None:
            problems.append(f"stop {i}: {place_id} no longer exists; choose another place.")
            continue
        stops.append(
            {
                "place_id": place_id,
                "name": hit.place.name,
                "category": hit.place.category,
                "lat": round(hit.lat, 6),
                "lon": round(hit.lon, 6),
                "minutes": stop.minutes,
                "why": stop.why,
            }
        )

    covered = {s["category"] for s in stops}
    missing = [c for c in plan.categories if c not in covered]
    if missing and len(stops) == len(plan.stops):
        problems.append(
            f"categories: no stop is a {' or '.join(missing)}. Add one, or search wider."
        )

    # Legs, and the timetable.
    total_walk_m = 0
    total_minutes = 0
    here = (plan.start.lat, plan.start.lon)
    here_name = plan.start.name
    for stop in stops:
        metres = distance_m(*here, stop["lat"], stop["lon"])
        if metres > MAX_LEG_M:
            problems.append(
                f"the walk from {here_name} to {stop['name']} is about "
                f"{metres * DETOUR_FACTOR / 1000:.1f} km; choose closer places."
            )
        stop["walk_m"] = round(metres * DETOUR_FACTOR)
        stop["walk_minutes"] = walk_minutes(metres)
        total_walk_m += stop["walk_m"]
        total_minutes += stop["walk_minutes"] + stop["minutes"]
        here, here_name = (stop["lat"], stop["lon"]), stop["name"] or "the last stop"
    back: dict[str, Any] | None = None
    if plan.return_to_start and stops:
        metres = distance_m(*here, plan.start.lat, plan.start.lon)
        back = {"walk_m": round(metres * DETOUR_FACTOR), "walk_minutes": walk_minutes(metres)}
        total_walk_m += back["walk_m"]
        total_minutes += back["walk_minutes"]

    if plan.max_minutes is not None and total_minutes > plan.max_minutes:
        problems.append(
            f"the plan takes about {total_minutes} minutes, over the {plan.max_minutes} "
            "asked for. Drop a stop, shorten visits, or choose closer places."
        )
    if plan.end_by is not None and plan.start_time is None:
        problems.append("start_time: needed to check the plan ends by end_by; assume one.")
    if plan.start_time is not None:
        clock = _minutes(plan.start_time)
        for stop in stops:
            clock += stop["walk_minutes"]
            stop["arrive"] = _clock(clock % (24 * 60))
            clock += stop["minutes"]
            stop["leave"] = _clock(clock % (24 * 60))
        if back is not None:
            clock += back["walk_minutes"]
            back["arrive"] = _clock(clock % (24 * 60))
        if plan.end_by is not None and clock > _minutes(plan.end_by):
            problems.append(
                f"the plan ends at about {_clock(clock % (24 * 60))}, after end_by "
                f"({plan.end_by}). Drop a stop, shorten visits, or choose closer places."
            )
        elif clock >= 24 * 60:
            problems.append("the plan runs past midnight.")

    if problems:
        return CheckResult(ok=False, problems=problems)
    return CheckResult(
        ok=True,
        plan={
            "title": plan.title,
            "summary": plan.summary,
            "categories": list(plan.categories),
            "start": plan.start.model_dump(),
            "start_time": plan.start_time,
            "end_by": plan.end_by,
            "max_minutes": plan.max_minutes,
            "stops": stops,
            "return": back,
            "total_walk_m": total_walk_m,
            "total_minutes": total_minutes,
            "note": ESTIMATE_NOTE,
        },
    )


async def run_trip_tool(ctx: ToolContext, name: str, raw_input: object) -> ToolOutcome:
    if name not in TOOL_NAMES:  # e.g. save_place: not offered to the planner
        message = f"Unknown tool: {name}"
        return ToolOutcome(json.dumps({"error": message}), is_error=True, summary=message)
    return await run_tool(ctx, name, raw_input)


TRIP_PLANNER = AgentTask(
    name="trip_planner",
    system=SYSTEM_PROMPT,
    tools=TRIP_TOOLS,
    finish_tool="submit_plan",
    run_tool=run_trip_tool,
    check=check_plan,
)
