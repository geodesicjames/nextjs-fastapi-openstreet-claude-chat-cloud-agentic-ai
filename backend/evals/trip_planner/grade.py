"""Grade a trip-planner run against its case's goal. Code only, no judge model.

The app's own check (check_plan in app/services/trip_planner.py) already
guarantees a passing plan is *valid*: real places from this run's searches,
legs under 4 km, times adding up, and whatever limits the model wrote into
the plan. What it can't know is whether the model wrote down what the user
actually asked for. A plan for the wrong neighbourhood, missing a requested
café, or with the "back by 4pm" quietly dropped is valid but wrong.

So a case passes (`success`) when the run produced a plan that passed the
check AND the plan matches the goal's expectations:

- near:            the start is within START_RADIUS_M of the named place
- categories:      every one is visited by at least one stop
- min_stops:       at least this many stops (e.g. "three cafés")
- return_to_start: the plan walks back to the start
- max_minutes:     the plan's total time (walking + stops) is within it
- start_time:      the plan starts at this time
- end_by:          the plan records this deadline and finishes by it

`first_try` is the stricter version: success with no failed check along
the way, i.e. the model's first proposal was already valid.
"""

from dataclasses import dataclass, field
from typing import Any

from app.services.chat_tools import distance_m

START_RADIUS_M = 500


@dataclass
class TripGrade:
    success: bool
    first_try: bool
    failures: list[str] = field(default_factory=list)


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def coords_for(name: str, gazetteer: dict[str, tuple[float, float]]) -> tuple[float, float]:
    """A case's "near" ("Arthur's Seat", "Meadows") -> the gazetteer's coordinates."""
    key = name.lower()
    return next(v for alias, v in gazetteer.items() if alias in key)


def finish_time(plan: dict[str, Any]) -> str | None:
    """When the plan ends: back at the start if it returns there, else leaving the last stop."""
    back = plan.get("return")
    if back and back.get("arrive"):
        return back["arrive"]
    stops = plan.get("stops") or []
    return stops[-1].get("leave") if stops else None


def grade_run(
    expect: dict[str, Any],
    status: str,
    error: str | None,
    plan: dict[str, Any] | None,
    failed_checks: int,
    gazetteer: dict[str, tuple[float, float]],
) -> TripGrade:
    if status != "awaiting_approval" or plan is None:
        reason = f"{status}, {error}" if error else status
        return TripGrade(False, False, [f"no plan passed the check ({reason})"])

    failures: list[str] = []
    stops = plan["stops"]

    if "near" in expect:
        lat, lon = coords_for(expect["near"], gazetteer)
        start = plan["start"]
        off = distance_m(lat, lon, start["lat"], start["lon"])
        if off > START_RADIUS_M:
            failures.append(f"starts {off:.0f} m from {expect['near']}")

    visited = {s["category"] for s in stops}
    missing = [c for c in expect.get("categories", []) if c not in visited]
    if missing:
        failures.append(f"no stop for {', '.join(missing)}")

    if len(stops) < expect.get("min_stops", 0):
        failures.append(f"{len(stops)} stops, wanted at least {expect['min_stops']}")

    if expect.get("return_to_start") and not plan.get("return"):
        failures.append("doesn't return to the start")

    if "max_minutes" in expect and plan["total_minutes"] > expect["max_minutes"]:
        failures.append(f"takes {plan['total_minutes']} min, over {expect['max_minutes']}")

    if "start_time" in expect and plan.get("start_time") != expect["start_time"]:
        failures.append(f"starts at {plan.get('start_time')}, not {expect['start_time']}")

    if "end_by" in expect:
        if plan.get("end_by") != expect["end_by"]:
            failures.append(f"end_by is {plan.get('end_by')}, not {expect['end_by']}")
        finish = finish_time(plan)
        if finish is None or _minutes(finish) > _minutes(expect["end_by"]):
            failures.append(f"finishes at {finish}, after {expect['end_by']}")

    success = not failures
    return TripGrade(success, success and failed_checks == 0, failures)
