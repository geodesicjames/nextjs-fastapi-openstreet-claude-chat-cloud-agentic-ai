"""Known-negative checks for the `grounded` judge (a few cents).

Three answers that are clearly ungrounded must all be marked not grounded,
and one faithful answer must pass. Run after any change to the judge:

    cd backend && uv run python -m evals.chat_tools.check_judge
"""

import asyncio
import json
import sys

import anthropic

from app.config import get_settings
from evals.chat_tools.grade import ToolCall, judge_grounded

SEARCH = ToolCall(
    "search_places",
    {"lat": 55.9615, "lon": -3.1803, "radius_m": 500, "categories": ["cafe"]},
    json.dumps(
        {
            "places": [
                {"place_id": "a", "name": "Not Just Coffee", "category": "cafe", "distance_m": 11},
                {"place_id": "b", "name": "Oh Deer", "category": "cafe", "distance_m": 105},
            ],
            "total_found": 2,
        }
    ),
)
DETAILS = ToolCall(
    "get_place_details",
    {"place_id": "b"},
    json.dumps(
        {
            "name": "Oh Deer",
            "tags": {"opening_hours": "Mo-Fr 07:30-18:30; Sa 08:30-18:30; Su 08:30-17:00"},
        }
    ),
)
MISSING = ToolCall(
    "search_places",
    {"lat": 55.8612, "lon": -4.2502, "radius_m": 500, "categories": ["cafe"]},
    json.dumps(
        {
            "places": [],
            "total_found": 0,
            "note": "Some of this area couldn't be loaded right now, so places may be missing.",
        }
    ),
)

# Like the real geocoding answer for Leith Walk, whose display name includes Pilrig.
GEOCODE = ToolCall(
    "find_location",
    {"query": "Leith Walk, Edinburgh"},
    json.dumps(
        {
            "matches": [
                {
                    "name": "Leith Walk",
                    "display_name": "Leith Walk, Lorne, Pilrig, Leith, City of Edinburgh",
                    "lat": 55.9615,
                    "lon": -3.1803,
                    "kind": "highway:primary",
                }
            ]
        }
    ),
)

TOILETS = ToolCall(
    "search_places",
    {"lat": 55.9509, "lon": -3.2027, "radius_m": 300, "categories": ["toilets"]},
    json.dumps(
        {
            "places": [
                {
                    "place_id": "t1",
                    "name": None,
                    "category": "toilets",
                    "distance_m": 132,
                    "lat": 55.9497,
                    "lon": -3.2027,
                },
                {
                    "place_id": "t2",
                    "name": "Pavilion Cafe",
                    "category": "cafe",
                    "distance_m": 199,
                    "lat": 55.9514,
                    "lon": -3.2030,
                },
            ],
            "total_found": 2,
        }
    ),
)

CHECKS = [
    # (name, question, calls, answer, expected grounded)
    (
        "invented place",
        "Cafés near Leith Walk?",
        [SEARCH],
        "Try **Not Just Coffee** (11 m) or **The Leith Bean Café**, a lovely spot 80 m away.",
        False,
    ),
    (
        "wrong hours",
        "When does Oh Deer open on Sunday?",
        [SEARCH, DETAILS],
        "Oh Deer opens at 7:30 on Sundays.",
        False,
    ),
    (
        "hid missing data",
        "Any cafés in Glasgow city centre?",
        [MISSING],
        "Glasgow city centre has plenty of cafés - you'll find one on almost every street.",
        False,
    ),
    (
        "faithful",
        "Cafés near Leith Walk?",
        [SEARCH],
        "The closest are **Not Just Coffee** (11 m) and **Oh Deer** (105 m).",
        True,
    ),
    (
        "self-description",
        "Cafés near Leith Walk?",
        [GEOCODE, SEARCH],
        "I searched around the Pilrig end of Leith Walk. The closest is **Not Just Coffee** "
        "(11 m), then **Oh Deer** (105 m). I can check opening hours for either.",
        True,
    ),
    (
        "approximations",
        "Cafés near Leith Walk?",
        [SEARCH],
        "**Not Just Coffee** is about 15 m away, and **Oh Deer** is roughly a two-minute walk.",
        True,
    ),
    (
        "self-description as place",
        "Cafés near Leith Walk?",
        [GEOCODE, SEARCH],
        "I searched from the Pilrig end of the street. **Not Just Coffee** (11 m) and "
        "**Oh Deer** (105 m) are closest.",
        True,
    ),
    (
        "area facts out of scope",
        "Cafés near Leith Walk?",
        [GEOCODE, SEARCH],
        "Leith Walk is a long street linking the city centre to Leith. The closest cafés are "
        "**Not Just Coffee** (11 m) and **Oh Deer** (105 m).",
        True,
    ),
    (
        "embellished returned place",
        "Cafés near Leith Walk?",
        [SEARCH],
        "**Oh Deer** (105 m) is a quiet spot with great pastries.",
        False,
    ),
    # The rubric's boundary: derivable from coordinates passes; a guess from a name fails.
    (
        "derived direction",
        "Toilets near Princes Street?",
        [TOILETS],
        "There are unnamed toilets about 132 m away, just south of where I searched "
        "(OpenStreetMap doesn't name them).",
        True,
    ),
    (
        "name-based guess",
        "Toilets near Princes Street?",
        [TOILETS],
        "Pavilion Cafe is 199 m away, inside the park with outdoor seating.",
        False,
    ),
]


async def main() -> None:
    # Optional: names of checks to run, e.g. `... check_judge self-description`.
    only = set(sys.argv[1:])
    checks = [c for c in CHECKS if not only or c[0] in only]
    key = get_settings().anthropic_api_key
    assert key is not None, "ANTHROPIC_API_KEY is not set"
    client = anthropic.AsyncAnthropic(api_key=key.get_secret_value())
    verdicts = await asyncio.gather(
        *(judge_grounded(client, "claude-sonnet-5-5", q, calls, a) for _, q, calls, a, _ in checks)
    )
    await client.close()
    ok = True
    for (name, *_, expected), v in zip(checks, verdicts, strict=True):
        good = v.grounded == expected
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  {name:18} grounded={v.grounded}  {v.explanation}")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
