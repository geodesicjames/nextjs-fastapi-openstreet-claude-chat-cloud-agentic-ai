"""The chat tool loop: tools backed by real data, guardrails, saved places.

The model is the scripted FakeChatModel; the tools run for real against
PostGIS. Place data and geocoding answers are put in the caches up front so
no test calls Overpass or Nominatim (one uses respx to make Overpass fail).
"""

import itertools
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ChatMessage, GeocodeResult, LlmUsage, Place, PlaceCell, SavedPlace, User
from app.services import chat_tools
from app.services import places as places_service
from app.services.chat_tools import TOOLS
from app.services.grid import BBox, cells_for
from app.services.llm import ModelUnavailableError
from app.services.pricing import TokenUsage
from tests.chat_helpers import FakeChatModel, new_conversation, reply, send, tool_use, wants_tools
from tests.factories import grant, make_user, sign_in_as

# Leith Walk, Edinburgh, and places at known distances from it.
LEITH = {"lat": 55.9600, "lon": -3.1800}
NEAR_CAFE = {"name": "Bean There", "category": "cafe", "lat": 55.9610, "lon": -3.1800}  # ~111 m
FAR_CAFE = {"name": "Far Away Café", "category": "cafe", "lat": 55.9690, "lon": -3.1800}  # ~1 km
PARK = {"name": "Pilrig Park", "category": "park", "lat": 55.9605, "lon": -3.1810}  # ~85 m


@pytest.fixture
async def edinburgh(db: AsyncSession) -> dict[str, Place]:
    """Places around Leith Walk, with the area's grid cells marked as fresh."""
    now = datetime.now(UTC)
    rows: dict[str, Place] = {}
    for i, p in enumerate([NEAR_CAFE, FAR_CAFE, PARK]):
        place = Place(
            osm_type="node",
            osm_id=1000 + i,
            name=p["name"],
            category=p["category"],
            tags={"name": p["name"], "opening_hours": "Mo-Su 08:00-18:00"},
            geom=f"SRID=4326;POINT({p['lon']} {p['lat']})",
            fetched_at=now,
        )
        db.add(place)
        rows[p["name"]] = place
    area = BBox(west=-3.25, south=55.92, east=-3.11, north=56.00)
    db.add_all(PlaceCell(ix=ix, iy=iy, fetched_at=now) for ix, iy in cells_for(area))
    db.add(
        GeocodeResult(
            query="leith walk",
            results=[
                {
                    "name": "Leith Walk",
                    "display_name": "Leith Walk, Edinburgh, Scotland",
                    **LEITH,
                    "bbox": [-3.19, 55.95, -3.17, 55.97],
                    "kind": "highway:primary",
                }
            ],
            fetched_at=now,
        )
    )
    await db.commit()
    return rows


def tool_results(call: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool_result blocks in the last message the model was sent."""
    last = call["messages"][-1]
    assert last["role"] == "user"
    return [b for b in last["content"] if b["type"] == "tool_result"]


def result_json(block: dict[str, Any]) -> dict[str, Any]:
    return json.loads(block["content"])


# --- The loop -------------------------------------------------------------------------


async def test_find_then_search_then_answer(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
) -> None:
    fake_model.replies += [
        wants_tools(tool_use("find_location", {"query": "Leith Walk"})),
        wants_tools(
            tool_use(
                "search_places",
                {**LEITH, "radius_m": 500, "categories": ["cafe"]},
                id="toolu_2",
            )
        ),
        reply("Bean There is 111 m away."),
    ]
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "Coffee near Leith Walk?")

    assert [e for e, _ in events] == [
        "tool_start",
        "tool_result",
        "tool_start",
        "tool_result",
        "text",
        "done",
    ]
    assert events[0][1] == {"tool": "find_location", "input": {"query": "Leith Walk"}}
    assert events[1][1]["summary"] == "Found Leith Walk"
    search_result = events[3][1]
    assert search_result["ok"] is True
    assert [p["name"] for p in search_result["places"]] == [
        "Bean There"
    ]  # far café is outside 500 m

    # Every call got the tools; none was forced.
    assert len(fake_model.calls) == 3
    for call in fake_model.calls:
        assert [t["name"] for t in call["tools"]] == [t["name"] for t in TOOLS]
        assert call["tool_choice"] is None

    # The model saw the tool results, answered by tool_use id.
    [geo] = tool_results(fake_model.calls[1])
    assert geo["tool_use_id"] == "toolu_1"
    assert result_json(geo)["matches"][0]["lat"] == LEITH["lat"]
    [found] = tool_results(fake_model.calls[2])
    assert found["tool_use_id"] == "toolu_2"
    places = result_json(found)
    assert places["total_found"] == 1
    assert places["places"][0]["name"] == "Bean There"
    assert 100 < places["places"][0]["distance_m"] < 125

    # The whole turn is stored; the conversation view shows only the text.
    assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 6
    assert await db.scalar(select(func.count()).select_from(LlmUsage)) == 3
    detail = (await client.get(f"/chat/conversations/{conversation_id}")).json()
    assert [(m["role"], m["text"]) for m in detail["messages"]] == [
        ("user", "Coffee near Leith Walk?"),
        ("assistant", "Bean There is 111 m away."),
    ]


async def test_next_turn_replays_the_tool_exchange(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel, edinburgh: dict[str, Place]
) -> None:
    fake_model.replies += [
        wants_tools(tool_use("find_location", {"query": "Leith Walk"})),
        reply("Found it."),
        reply("You're welcome."),
    ]
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, "Where is Leith Walk?")
    await send(client, conversation_id, "Thanks")

    roles = [m["role"] for m in fake_model.calls[2]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]
    assert fake_model.calls[2]["messages"][1]["content"][1]["type"] == "tool_use"


async def test_parallel_tool_calls_answered_in_one_message(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel, edinburgh: dict[str, Place]
) -> None:
    near_id = str(edinburgh["Bean There"].id)
    park_id = str(edinburgh["Pilrig Park"].id)
    fake_model.replies += [
        wants_tools(
            tool_use("get_place_details", {"place_id": near_id}, id="a"),
            tool_use("get_place_details", {"place_id": park_id}, id="b"),
        ),
        reply("Both open at 8."),
    ]
    conversation_id = await new_conversation(client)

    await send(client, conversation_id, "When do they open?")

    results = tool_results(fake_model.calls[1])
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert result_json(results[0])["tags"]["opening_hours"] == "Mo-Su 08:00-18:00"


# --- Tool errors go back to the model ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "args", "error"),
    [
        ("search_places", {**LEITH, "radius_m": 50_000, "categories": ["cafe"]}, "radius_m"),
        ("search_places", {**LEITH, "radius_m": 500, "categories": []}, "categories"),
        ("get_place_details", {"place_id": "not-a-uuid"}, "place_id"),
        ("get_place_details", {"place_id": str(uuid.uuid4())}, "No place with that place_id"),
        ("no_such_tool", {}, "Unknown tool"),
    ],
)
async def test_bad_tool_calls_return_errors_to_the_model(
    client: AsyncClient,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
    name: str,
    args: dict[str, Any],
    error: str,
) -> None:
    fake_model.replies += [wants_tools(tool_use(name, args)), reply("Sorry, let me retry.")]
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "hi")

    [result] = tool_results(fake_model.calls[1])
    assert result["is_error"] is True
    assert error in result_json(result)["error"]
    assert dict(events)["tool_result"]["ok"] is False
    assert events[-1][0] == "done"


async def test_a_crashing_tool_is_reported_not_fatal(
    client: AsyncClient,
    chatter: User,
    fake_model: FakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setitem(chat_tools._HANDLERS, "find_location", (chat_tools.FindLocationIn, boom))
    fake_model.replies += [
        wants_tools(tool_use("find_location", {"query": "Leith"})),
        reply("Couldn't look that up."),
    ]
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "hi")

    assert result_json(tool_results(fake_model.calls[1])[0]) == {"error": "The tool failed."}
    assert events[-1][0] == "done"


async def test_search_reports_missing_data(
    client: AsyncClient,
    chatter: User,
    fake_model: FakeChatModel,
    respx_mock: respx.MockRouter,
) -> None:
    # No cached cells here, and Overpass is down.
    respx_mock.post(get_settings().overpass_url).mock(return_value=httpx.Response(500))
    fake_model.replies += [
        wants_tools(tool_use("search_places", {**LEITH, "radius_m": 300, "categories": ["park"]})),
        reply("I couldn't load that area."),
    ]
    conversation_id = await new_conversation(client)

    await send(client, conversation_id, "parks?")

    data = result_json(tool_results(fake_model.calls[1])[0])
    assert data["places"] == []
    assert "may be missing" in data["note"]


# --- Guardrails ---------------------------------------------------------------------------


async def test_tool_step_cap_forces_an_answer(
    client: AsyncClient,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
    use_settings: Any,
) -> None:
    use_settings(chat_max_tool_steps=2)
    look_up = tool_use("find_location", {"query": "Leith Walk"})
    fake_model.replies += [
        wants_tools(look_up),
        wants_tools(look_up),
        reply("Here's what I found."),
    ]
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "hi")

    assert [c["tool_choice"] for c in fake_model.calls] == [None, None, {"type": "none"}]
    assert events[-1][1]["stop_reason"] == "end_turn"


async def test_tool_call_after_the_cap_ends_the_turn(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
    use_settings: Any,
) -> None:
    use_settings(chat_max_tool_steps=1)
    look_up = tool_use("find_location", {"query": "Leith Walk"})
    fake_model.replies += [wants_tools(look_up), wants_tools(look_up)]  # ignores tool_choice none
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "hi")

    assert dict(events)["error"]["code"] == "truncated"
    assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0
    assert await db.scalar(select(func.count()).select_from(LlmUsage)) == 2  # both calls cost


async def test_budget_running_out_mid_turn_stops_the_loop(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
) -> None:
    # The first call alone costs more than the $1 budget.
    expensive = TokenUsage(input_tokens=0, output_tokens=2_000_000)  # 2M x $0.50 = $1.00
    fake_model.replies += [
        wants_tools(tool_use("find_location", {"query": "Leith Walk"}), usage=expensive),
        reply("never sent"),
    ]
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "hi")

    assert dict(events)["error"]["code"] == "budget"
    assert len(fake_model.calls) == 1
    assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0
    assert await db.scalar(select(func.sum(LlmUsage.cost_usd))) == Decimal("1.000000")


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_cut_off_tool_call_is_not_run_or_saved(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
    stop_reason: str,
) -> None:
    cut_off = reply(
        chunks=[],
        content=[tool_use("save_place", {"place_id": str(edinburgh["Bean There"].id), "note": ""})],
        stop_reason=stop_reason,
    )
    fake_model.replies.append(cut_off)
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "save it")

    assert "tool_start" not in dict(events)
    assert dict(events)["error"]["code"] == ("refused" if stop_reason == "refusal" else "truncated")
    assert await db.scalar(select(func.count()).select_from(SavedPlace)) == 0
    assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0


async def test_api_failure_mid_turn_keeps_usage_drops_messages(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
) -> None:
    fake_model.replies += [
        wants_tools(tool_use("find_location", {"query": "Leith Walk"})),
        ModelUnavailableError("api_error"),
    ]
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "hi")

    assert dict(events)["error"]["code"] == "unavailable"
    assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0
    assert await db.scalar(select(func.count()).select_from(LlmUsage)) == 1


# --- Saved places -------------------------------------------------------------------------


async def test_save_place_and_manage_saved_places(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
) -> None:
    cafe_id = str(edinburgh["Bean There"].id)
    fake_model.replies += [
        wants_tools(tool_use("save_place", {"place_id": cafe_id, "note": "try the flat white"})),
        reply("Saved."),
        wants_tools(tool_use("save_place", {"place_id": cafe_id, "note": "great cake"})),
        reply("Updated."),
    ]
    conversation_id = await new_conversation(client)

    await send(client, conversation_id, "save Bean There")
    await send(client, conversation_id, "note: great cake")  # saving again updates the note

    saved = (await client.get("/saved-places")).json()
    assert [(s["name"], s["note"], s["category"]) for s in saved] == [
        ("Bean There", "great cake", "cafe")
    ]
    assert saved[0]["lat"] == pytest.approx(NEAR_CAFE["lat"])

    # Another user can't see or delete it.
    other = await make_user(db, "other@example.com")
    await grant(db, other)
    await sign_in_as(db, client, other)
    assert (await client.get("/saved-places")).json() == []
    assert (await client.delete(f"/saved-places/{saved[0]['id']}")).status_code == 404

    await sign_in_as(db, client, chatter)
    assert (await client.delete(f"/saved-places/{saved[0]['id']}")).status_code == 204
    assert (await client.get("/saved-places")).json() == []


async def test_saved_places_need_sign_in(client: AsyncClient) -> None:
    assert (await client.get("/saved-places")).status_code == 401


# --- Name filter (issue #29) ----------------------------------------------------------------


_osm_ids = itertools.count(5000)


async def add_cafes(db: AsyncSession, specs: list[tuple[str, float]]) -> None:
    """Cafés due north of LEITH at the given distances (metres)."""
    now = datetime.now(UTC)
    for name, metres in specs:
        lat = LEITH["lat"] + metres / 111_320
        db.add(
            Place(
                osm_type="node",
                osm_id=next(_osm_ids),
                name=name,
                category="cafe",
                tags={"name": name},
                geom=f"SRID=4326;POINT({LEITH['lon']} {lat})",
                fetched_at=now,
            )
        )
    await db.commit()


async def search_result(
    client: AsyncClient, fake_model: FakeChatModel, args: dict[str, Any]
) -> dict[str, Any]:
    fake_model.replies += [wants_tools(tool_use("search_places", args)), reply("ok")]
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, "find it")
    return result_json(tool_results(fake_model.calls[-1])[0])


async def test_name_finds_a_place_beyond_the_nearest_twenty(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
) -> None:
    # The eval's d02 case: 25 cafés nearer than the one asked about.
    await add_cafes(db, [(f"Cafe {i:02}", 10 + i * 10) for i in range(25)])
    await add_cafes(db, [("The Whispering Kettle", 600)])
    args = {**LEITH, "radius_m": 1000, "categories": ["cafe"]}

    unnamed = await search_result(client, fake_model, {**args, "name": None})
    assert "The Whispering Kettle" not in [p["name"] for p in unnamed["places"]]

    named = await search_result(client, fake_model, {**args, "name": "whispering KETTLE"})
    assert [p["name"] for p in named["places"]] == ["The Whispering Kettle"]


async def test_name_ignores_accents(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
) -> None:
    await add_cafes(db, [("Söderberg", 200)])
    found = await search_result(
        client, fake_model, {**LEITH, "radius_m": 500, "categories": ["cafe"], "name": "Soderberg"}
    )
    assert [p["name"] for p in found["places"]] == ["Söderberg"]


async def test_name_wildcards_are_literal(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
) -> None:
    await add_cafes(db, [("100% Coffee", 100), ("Big_Cup", 150)])
    args = {**LEITH, "radius_m": 500, "categories": ["cafe"]}

    pct = await search_result(client, fake_model, {**args, "name": "%"})
    assert [p["name"] for p in pct["places"]] == ["100% Coffee"]  # not every café
    under = await search_result(client, fake_model, {**args, "name": "g_c"})
    assert [p["name"] for p in under["places"]] == ["Big_Cup"]


async def test_no_name_match_says_so(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel, edinburgh: dict[str, Place]
) -> None:
    found = await search_result(
        client,
        fake_model,
        {**LEITH, "radius_m": 500, "categories": ["cafe"], "name": "Nowhere Cafe"},
    )
    assert found["places"] == []
    assert "No cafe matching 'Nowhere Cafe' within 500 m" in found["note"]


@pytest.mark.parametrize("name", ["", "   "])
async def test_blank_name_means_no_filter(
    client: AsyncClient,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
    name: str,
) -> None:
    found = await search_result(
        client, fake_model, {**LEITH, "radius_m": 500, "categories": ["cafe"], "name": name}
    )
    assert [p["name"] for p in found["places"]] == ["Bean There"]


async def test_overlong_name_is_rejected(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel, edinburgh: dict[str, Place]
) -> None:
    fake_model.replies += [
        wants_tools(
            tool_use(
                "search_places",
                {**LEITH, "radius_m": 500, "categories": ["cafe"], "name": "x" * 101},
            )
        ),
        reply("ok"),
    ]
    await send(client, await new_conversation(client), "find it")
    [result] = tool_results(fake_model.calls[-1])
    assert result["is_error"] is True
    assert "name" in result_json(result)["error"]


async def test_nearest_places_survive_the_result_cap(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    edinburgh: dict[str, Place],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With more matches than the cap, the nearest must be kept, not the
    # alphabetically first (the bug the name filter work uncovered).
    monkeypatch.setattr(places_service, "MAX_RESULTS", 2)
    await add_cafes(db, [("Zebra Cafe", 20), ("Yak Cafe", 40), ("Aardvark Cafe", 900)])
    found = await search_result(
        client, fake_model, {**LEITH, "radius_m": 1000, "categories": ["cafe"], "name": None}
    )
    assert [p["name"] for p in found["places"]] == ["Zebra Cafe", "Yak Cafe"]
