"""The trip-planner eval's grader (evals/trip_planner/grade.py): code only, so it runs in CI."""

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from evals.trip_planner.grade import coords_for, finish_time, grade_run

EVALS = Path(__file__).resolve().parents[1] / "evals"
GAZETTEER = {"leith walk": (55.9600, -3.1826), "meadows": (55.9423, -3.1943)}


def a_plan(**changes: Any) -> dict[str, Any]:
    """A checked plan as check_plan stores it: from Leith Walk, café then park, and back."""
    plan = {
        "start": {"name": "Leith Walk", "lat": 55.9601, "lon": -3.1825},
        "start_time": "14:00",
        "end_by": "16:00",
        "stops": [
            {"category": "cafe", "arrive": "14:05", "leave": "14:35"},
            {"category": "park", "arrive": "14:45", "leave": "15:15"},
        ],
        "return": {"walk_m": 600, "walk_minutes": 10, "arrive": "15:25"},
        "total_minutes": 85,
    }
    return plan | changes


EXPECT = {
    "near": "Leith Walk",
    "categories": ["cafe", "park"],
    "return_to_start": True,
    "max_minutes": 90,
    "start_time": "14:00",
    "end_by": "16:00",
}


def grade(plan: dict[str, Any] | None, *, expect: dict[str, Any] = EXPECT, failed_checks: int = 0):
    status = "awaiting_approval" if plan else "failed"
    return grade_run(expect, status, None if plan else "step_limit", plan, failed_checks, GAZETTEER)


def test_a_plan_that_meets_the_goal_passes() -> None:
    result = grade(a_plan())
    assert result.success and result.first_try and result.failures == []


def test_first_try_needs_no_failed_checks() -> None:
    result = grade(a_plan(), failed_checks=2)
    assert result.success and not result.first_try


def test_no_plan_fails_with_the_reason() -> None:
    result = grade(None)
    assert not result.success
    assert result.failures == ["no plan passed the check (failed, step_limit)"]


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"start": {"name": "Meadows", "lat": 55.9423, "lon": -3.1943}}, "m from Leith Walk"),
        (
            {"stops": [{"category": "cafe", "arrive": "14:05", "leave": "14:35"}]},
            "no stop for park",
        ),
        ({"return": None}, "doesn't return to the start"),
        ({"total_minutes": 120}, "takes 120 min, over 90"),
        ({"start_time": "15:00"}, "starts at 15:00, not 14:00"),
        ({"end_by": None}, "end_by is None, not 16:00"),
        ({"return": {"walk_m": 600, "walk_minutes": 10, "arrive": "16:20"}}, "finishes at 16:20"),
    ],
)
def test_plans_that_miss_the_goal_fail(changes: dict[str, Any], failure: str) -> None:
    result = grade(a_plan(**changes))
    assert not result.success and not result.first_try
    assert any(failure in f for f in result.failures), result.failures


def test_min_stops() -> None:
    result = grade(a_plan(), expect={"categories": ["cafe"], "min_stops": 3})
    assert result.failures == ["2 stops, wanted at least 3"]


def test_the_finish_is_back_at_the_start_or_leaving_the_last_stop() -> None:
    assert finish_time(a_plan()) == "15:25"
    assert finish_time(a_plan(**{"return": None})) == "15:15"
    assert (
        finish_time(a_plan(start_time=None, stops=[{"category": "cafe"}], **{"return": None}))
        is None
    )


def test_every_case_is_well_formed() -> None:
    fixture = json.loads(gzip.decompress((EVALS / "chat_tools" / "fixture.json.gz").read_bytes()))
    gazetteer = {
        alias: (e["results"][0]["lat"], e["results"][0]["lon"])
        for e in fixture["gazetteer"]
        for alias in e["aliases"]
    }
    cases = json.loads((EVALS / "trip_planner" / "cases.json").read_text())
    known = {
        "near",
        "categories",
        "min_stops",
        "return_to_start",
        "max_minutes",
        "start_time",
        "end_by",
    }
    assert len({c["id"] for c in cases}) == len(cases)
    for case in cases:
        assert set(case["expect"]) <= known, case["id"]
        coords_for(case["expect"]["near"], gazetteer)  # raises if unknown
