"""Oracle and null checks for the chat tool-use eval's code grader.

Run before any paid eval pass (and in CI): a correct trajectory for every
case must pass, and making no tool calls must fail every case that needs
tools. If either breaks, the grader is wrong, not the model.
"""

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from evals.chat_tools.grade import ToolCall, grade_tool_use

EVAL_DIR = Path(__file__).resolve().parents[1] / "evals" / "chat_tools"
CASES = json.loads((EVAL_DIR / "cases.json").read_text())
FIXTURE = json.loads(gzip.decompress((EVAL_DIR / "fixture.json.gz").read_bytes()))
ALIASES = {
    a: (e["results"][0]["lat"], e["results"][0]["lon"])
    for e in FIXTURE["gazetteer"]
    for a in e["aliases"]
}


def coords(name: str) -> tuple[float, float]:
    return next(v for alias, v in ALIASES.items() if alias in name.lower())


GAZETTEER = {
    c["expect"]["near"].lower(): coords(c["expect"]["near"]) for c in CASES if "near" in c["expect"]
}
PLACE_IDS = {
    "Oh Deer": "id-oh-deer",
    "The Whispering Kettle": "id-kettle",
    "Soderberg": "id-soderberg",
}
NAMES: dict[str, str | None] = {v: k for k, v in PLACE_IDS.items()}


def oracle(expect: dict[str, Any]) -> list[ToolCall]:
    """A correct trajectory for the case's expectations."""
    if expect.get("no_tools"):
        return []
    calls: list[ToolCall] = []
    if "near" in expect or "require" in expect:
        calls.append(ToolCall("find_location", {"query": expect.get("near", "somewhere")}, "{}"))
    if "near" in expect:
        lat, lon = GAZETTEER[expect["near"].lower()]
        categories = expect.get("categories_all") or expect["categories_any"][:1]
        radius = min(500, expect.get("max_radius_m", 500))
        calls.append(
            ToolCall(
                "search_places",
                {"lat": lat, "lon": lon, "radius_m": radius, "categories": categories},
                "{}",
            )
        )
    if "details_of" in expect:
        calls.append(
            ToolCall("get_place_details", {"place_id": PLACE_IDS[expect["details_of"]]}, "{}")
        )
    if "saves" in expect:
        note = expect.get("note_contains", "")
        calls.append(
            ToolCall("save_place", {"place_id": PLACE_IDS[expect["saves"]], "note": note}, "{}")
        )
    for tool in expect.get("require", []):
        if tool not in {c.name for c in calls}:
            calls.append(ToolCall(tool, {"place_id": "any", "note": ""}, "{}"))
    return calls


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_oracle_passes(case: dict[str, Any]) -> None:
    grade = grade_tool_use(case["expect"], oracle(case["expect"]), GAZETTEER, NAMES)
    assert grade.passed, grade.failures


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_no_tool_calls_fails_unless_none_expected(case: dict[str, Any]) -> None:
    grade = grade_tool_use(case["expect"], [], GAZETTEER, NAMES)
    assert grade.passed is bool(case["expect"].get("no_tools")), grade.failures


def test_wrong_trajectories_fail() -> None:
    expect = next(c for c in CASES if c["id"] == "s01")["expect"]  # cafés near Leith Walk
    lat, lon = GAZETTEER["leith walk"]
    far_lat, far_lon = GAZETTEER["the meadows"]

    def search(**changes: Any) -> list[ToolCall]:
        args = {"lat": lat, "lon": lon, "radius_m": 500, "categories": ["cafe"], **changes}
        return [ToolCall("search_places", args, "{}")]

    assert not grade_tool_use(expect, search(lat=far_lat, lon=far_lon), GAZETTEER, NAMES).passed
    assert not grade_tool_use(expect, search(categories=["park"]), GAZETTEER, NAMES).passed
    assert not grade_tool_use(expect, search(radius_m=2000), GAZETTEER, NAMES).passed
    saved = search() + [ToolCall("save_place", {"place_id": "x", "note": ""}, "{}")]
    assert not grade_tool_use(expect, saved, GAZETTEER, NAMES).passed
    # Any extra, valid search doesn't hurt: properties, not an exact sequence.
    assert grade_tool_use(expect, search(categories=["park"]) + search(), GAZETTEER, NAMES).passed


def test_wrong_place_or_note_fails() -> None:
    details = next(c for c in CASES if c["id"] == "d01")["expect"]
    wrong = [ToolCall("get_place_details", {"place_id": PLACE_IDS["Soderberg"]}, "{}")]
    assert not grade_tool_use(details, wrong, GAZETTEER, NAMES).passed

    save = next(c for c in CASES if c["id"] == "v02")["expect"]
    no_note = [ToolCall("save_place", {"place_id": PLACE_IDS["Oh Deer"], "note": "lunch"}, "{}")]
    assert not grade_tool_use(save, no_note, GAZETTEER, NAMES).passed
