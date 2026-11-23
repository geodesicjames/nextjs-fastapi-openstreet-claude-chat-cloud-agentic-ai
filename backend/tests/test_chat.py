"""Chat conversations and streamed replies, with a scripted fake model.

See tests/chat_helpers.py for FakeChatModel. The real SDK path is covered by
the opt-in live test in test_chat_live.py.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatMessage, LlmUsage, Place, User
from app.services import chat as chat_service
from app.services.llm import ModelUnavailableError
from app.services.pricing import TokenUsage
from tests.chat_helpers import (
    THINKING,
    FakeChatModel,
    new_conversation,
    reply,
    send,
    tool_use,
    wants_tools,
)
from tests.factories import grant, make_user, sign_in_as, signed_in_user

# --- Access -------------------------------------------------------------------------


async def test_chat_needs_sign_in_and_a_grant(
    client: AsyncClient, db: AsyncSession, fake_model: FakeChatModel
) -> None:
    assert (await client.post("/chat/conversations")).status_code == 401
    await signed_in_user(db, client)
    assert (await client.post("/chat/conversations")).status_code == 403


async def test_budget_used_up_blocks_sending(
    client: AsyncClient, db: AsyncSession, chatter: User, fake_model: FakeChatModel
) -> None:
    conversation_id = await new_conversation(client)
    db.add(
        LlmUsage(
            user_id=chatter.id,
            model="claude-haiku-5-5",
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal("1.00"),
        )
    )
    await db.commit()

    response = await client.post(
        f"/chat/conversations/{conversation_id}/messages", json={"text": "hi"}
    )

    assert response.status_code == 429
    assert fake_model.calls == []


async def test_no_api_key_is_503(
    app: FastAPI, client: AsyncClient, chatter: User, use_settings: Callable[..., None]
) -> None:
    use_settings(anthropic_api_key=None)  # and no fake model override
    conversation_id = await new_conversation(client)

    response = await client.post(
        f"/chat/conversations/{conversation_id}/messages", json={"text": "hi"}
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Chat isn't configured on this server."


# --- A turn ---------------------------------------------------------------------------


async def test_reply_streams_and_is_saved(
    client: AsyncClient, db: AsyncSession, chatter: User, fake_model: FakeChatModel
) -> None:
    fake_model.replies.append(reply("Try the Meadows.", chunks=["Try the ", "Meadows."]))
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "Where can I walk my dog?")

    assert [e for e, _ in events] == ["text", "text", "done"]
    assert "".join(d["text"] for e, d in events if e == "text") == "Try the Meadows."
    done = events[-1][1]
    assert done["stop_reason"] == "end_turn"
    assert done["truncated"] is False
    # Haiku 5.5: 1,000 input x $0.10/M + 200 output x $0.50/M
    assert done["cost_usd"] == pytest.approx(0.0002)

    # The request: configured model and limits, stable system prompt.
    [call] = fake_model.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["max_tokens"] == 16_000
    assert call["effort"] == "low"
    assert call["system"] == chat_service.SYSTEM_PROMPT
    assert call["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Where can I walk my dog?"}]}
    ]

    # Saved: both messages, the conversation title, and the usage row.
    detail = (await client.get(f"/chat/conversations/{conversation_id}")).json()
    assert detail["title"] == "Where can I walk my dog?"
    assert [(m["role"], m["text"]) for m in detail["messages"]] == [
        ("user", "Where can I walk my dog?"),
        ("assistant", "Try the Meadows."),  # thinking blocks aren't shown
    ]
    assert detail["messages"][1]["id"] == done["message_id"]
    usage = await db.scalar(select(LlmUsage))
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.stop_reason) == (1_000, 200, "end_turn")
    assert str(usage.conversation_id) == conversation_id


async def test_history_is_replayed_exactly(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel
) -> None:
    first_reply = [THINKING, {"type": "text", "text": "Hi! How can I help?"}]
    fake_model.replies += [reply(content=first_reply), reply("Sure.")]
    conversation_id = await new_conversation(client)

    await send(client, conversation_id, "Hello")
    await send(client, conversation_id, "Find me a café")

    # The second request carries the first turn unchanged, thinking block
    # (empty text + signature) included, then the new message.
    assert fake_model.calls[1]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        {"role": "assistant", "content": first_reply},
        {"role": "user", "content": [{"type": "text", "text": "Find me a café"}]},
    ]


async def test_spend_counts_towards_the_budget(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel
) -> None:
    fake_model.replies.append(reply(usage=TokenUsage(input_tokens=10_000, output_tokens=20_000)))
    conversation_id = await new_conversation(client)

    await send(client, conversation_id, "hi")

    access = (await client.get("/chat/access")).json()
    assert access["spent_today_usd"] == pytest.approx(0.011)  # $0.001 + $0.010


async def test_truncated_reply_is_flagged_and_kept(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel
) -> None:
    fake_model.replies.append(reply("A very long", stop_reason="max_tokens"))
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "Tell me everything")

    assert events[-1][1]["truncated"] is True
    detail = (await client.get(f"/chat/conversations/{conversation_id}")).json()
    assert len(detail["messages"]) == 2


async def test_refusal_without_content_is_not_added_to_history(
    client: AsyncClient, db: AsyncSession, chatter: User, fake_model: FakeChatModel
) -> None:
    fake_model.replies.append(reply(chunks=[], content=[], stop_reason="refusal"))
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "something disallowed")

    assert [e for e, _ in events] == ["error", "done"]
    assert events[0][1]["code"] == "refused"
    assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0
    # The call still cost money, so it's still logged.
    assert await db.scalar(select(func.count()).select_from(LlmUsage)) == 1


async def test_refusal_with_partial_content_is_kept(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel
) -> None:
    fake_model.replies.append(reply("I started to", stop_reason="refusal"))
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "something")

    assert (
        "error",
        {"code": "refused", "message": "Sorry, I can't help with that request."},
    ) in events
    detail = (await client.get(f"/chat/conversations/{conversation_id}")).json()
    assert len(detail["messages"]) == 2  # append-only: what the model wrote is kept


async def test_api_failure_saves_nothing(
    client: AsyncClient, db: AsyncSession, chatter: User, fake_model: FakeChatModel
) -> None:
    fake_model.replies.append(ModelUnavailableError("api_error"))
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "hi")

    # Every turn ends with `done`, even after an error, so the client always
    # knows the stream is over (and what the turn cost).
    assert events == [
        (
            "error",
            {
                "code": "unavailable",
                "message": "The assistant is unavailable right now. Please try again.",
            },
        ),
        (
            "done",
            {
                "message_id": None,
                "user_message_id": None,
                "stop_reason": None,
                "truncated": False,
                "cost_usd": 0.0,
            },
        ),
    ]
    assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0
    assert await db.scalar(select(func.count()).select_from(LlmUsage)) == 0

    # The conversation still works afterwards (the lock was released).
    fake_model.replies.append(reply("Back again."))
    assert (await send(client, conversation_id, "hi"))[-1][0] == "done"


async def test_busy_conversation(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def locked(*_: Any) -> bool:
        return False

    monkeypatch.setattr(chat_service, "_lock", locked)
    conversation_id = await new_conversation(client)

    events = await send(client, conversation_id, "hi")

    assert events[0][1]["code"] == "busy"
    assert fake_model.calls == []


# --- Conversations ---------------------------------------------------------------------------


async def test_conversations_are_private(
    client: AsyncClient, db: AsyncSession, chatter: User, fake_model: FakeChatModel
) -> None:
    conversation_id = await new_conversation(client)
    other = await make_user(db, "other@example.com")
    await grant(db, other)
    await sign_in_as(db, client, other)

    assert (await client.get("/chat/conversations")).json() == []
    assert (await client.get(f"/chat/conversations/{conversation_id}")).status_code == 404
    response = await client.post(
        f"/chat/conversations/{conversation_id}/messages", json={"text": "hi"}
    )
    assert response.status_code == 404
    assert fake_model.calls == []


async def test_list_is_most_recent_first(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel
) -> None:
    fake_model.replies += [reply(), reply()]
    first = await new_conversation(client)
    second = await new_conversation(client)
    await send(client, second, "second")
    await send(client, first, "first")  # now the most recently active

    listed = (await client.get("/chat/conversations")).json()
    assert [c["id"] for c in listed] == [first, second]


@pytest.mark.parametrize("text", ["", "x" * 2_001])
async def test_message_length_limits(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel, text: str
) -> None:
    conversation_id = await new_conversation(client)
    response = await client.post(
        f"/chat/conversations/{conversation_id}/messages", json={"text": text}
    )
    assert response.status_code == 422
    assert fake_model.calls == []


# --- Map view -------------------------------------------------------------------------


async def test_map_view_goes_to_the_model_but_not_into_the_transcript(
    client: AsyncClient, db: AsyncSession, chatter: User, fake_model: FakeChatModel
) -> None:
    place = Place(
        osm_type="node",
        osm_id=1802,
        name="1802 Lounge",
        category="restaurant",
        tags={"name": "1802 Lounge"},
        geom="SRID=4326;POINT(-3.18 55.95)",
        fetched_at=datetime.now(UTC),
    )
    db.add(place)
    await db.commit()
    fake_model.replies.append(reply("It's a restaurant."))
    conversation_id = await new_conversation(client)

    view = {"lat": 55.957, "lon": -3.1847, "radius_m": 1240, "selected_place_id": str(place.id)}
    response = await client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"text": "More details on 1802 Lounge?", "map": view},
    )
    assert response.status_code == 200

    # The model gets the view first, with the selected place's name and
    # category from the database, then the user's own words.
    [call] = fake_model.calls
    [message] = call["messages"]
    note, asked = message["content"]
    assert asked == {"type": "text", "text": "More details on 1802 Lounge?"}
    assert note["text"] == (
        "<map_view>\n"
        "The user's map is centred on 55.95700, -3.18470 and reaches about 1.2 km "
        "from the centre.\n"
        f"They have selected 1802 Lounge (restaurant, place_id {place.id}).\n"
        "</map_view>"
    )
    # The system prompt is unchanged, so it still caches.
    assert call["system"] == chat_service.SYSTEM_PROMPT

    # The transcript and the title show only what the user typed.
    detail = (await client.get(f"/chat/conversations/{conversation_id}")).json()
    assert detail["title"] == "More details on 1802 Lounge?"
    assert detail["messages"][0]["text"] == "More details on 1802 Lounge?"


async def test_map_view_ignores_an_unknown_selected_place(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel
) -> None:
    fake_model.replies.append(reply("Here's what's nearby."))
    conversation_id = await new_conversation(client)

    view = {"lat": 55.957, "lon": -3.1847, "radius_m": 400, "selected_place_id": str(uuid.uuid4())}
    response = await client.post(
        f"/chat/conversations/{conversation_id}/messages",
        json={"text": "Cafés near here?", "map": view},
    )
    assert response.status_code == 200

    note = fake_model.calls[0]["messages"][0]["content"][0]["text"]
    assert "reaches about 400 m from the centre" in note
    assert "selected" not in note


@pytest.mark.parametrize(
    "view",
    [
        {"lat": 91, "lon": 0, "radius_m": 100},
        {"lat": 0, "lon": -181, "radius_m": 100},
        {"lat": 0, "lon": 0, "radius_m": 0},
        {"lat": 0, "lon": 0, "radius_m": 100, "selected_place_id": "not-a-uuid"},
    ],
)
async def test_map_view_is_validated(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel, view: dict[str, Any]
) -> None:
    conversation_id = await new_conversation(client)
    response = await client.post(
        f"/chat/conversations/{conversation_id}/messages", json={"text": "Hi", "map": view}
    )
    assert response.status_code == 422
    assert fake_model.calls == []


# --- Deleting a question -----------------------------------------------------------------


async def ask(client: AsyncClient, conversation_id: str, text: str) -> str:
    """Send a message; return the saved question's id."""
    events = await send(client, conversation_id, text)
    done = events[-1][1]
    assert done["user_message_id"], events
    return done["user_message_id"]


async def test_delete_turn_forgets_the_question_and_its_answer(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel
) -> None:
    lookup = tool_use("get_place_details", {"place_id": str(uuid.uuid4())})
    fake_model.replies += [
        reply("Answer one."),
        wants_tools(lookup),  # the middle question uses a tool: several rows
        reply("Answer two."),
        reply("Answer three."),
        reply("Answer four."),
    ]
    conversation_id = await new_conversation(client)
    await ask(client, conversation_id, "Question one")
    second = await ask(client, conversation_id, "Question two")
    await ask(client, conversation_id, "Question three")

    response = await client.delete(f"/chat/conversations/{conversation_id}/messages/{second}")
    assert response.status_code == 204

    detail = (await client.get(f"/chat/conversations/{conversation_id}")).json()
    assert [m["text"] for m in detail["messages"]] == [
        "Question one",
        "Answer one.",
        "Question three",
        "Answer three.",
    ]

    # The next reply sees neither the question nor its tool call, and no
    # thinking blocks (their signatures were bound to the deleted turn).
    await ask(client, conversation_id, "Question four")
    sent = fake_model.calls[-1]["messages"]
    assert sent == [
        {"role": "user", "content": [{"type": "text", "text": "Question one"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Answer one."}]},
        {"role": "user", "content": [{"type": "text", "text": "Question three"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Answer three."}]},
        {"role": "user", "content": [{"type": "text", "text": "Question four"}]},
    ]
    # Saved after the last row, despite the gap the deletion left.
    detail = (await client.get(f"/chat/conversations/{conversation_id}")).json()
    assert detail["messages"][-1]["text"] == "Answer four."


async def test_deleting_the_first_question_changes_the_title(
    client: AsyncClient, chatter: User, fake_model: FakeChatModel
) -> None:
    fake_model.replies += [reply("One."), reply("Two.")]
    conversation_id = await new_conversation(client)
    first = await ask(client, conversation_id, "Where is 1802 Lounge?")
    second = await ask(client, conversation_id, "Any parks nearby?")

    await client.delete(f"/chat/conversations/{conversation_id}/messages/{first}")
    [listed] = (await client.get("/chat/conversations")).json()
    assert listed["title"] == "Any parks nearby?"

    await client.delete(f"/chat/conversations/{conversation_id}/messages/{second}")
    [listed] = (await client.get("/chat/conversations")).json()
    assert listed["title"] == "New conversation"


async def test_delete_turn_only_accepts_your_own_questions(
    client: AsyncClient,
    db: AsyncSession,
    chatter: User,
    fake_model: FakeChatModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_model.replies.append(reply("Answer."))
    conversation_id = await new_conversation(client)
    question = await ask(client, conversation_id, "Question")
    answer = (await client.get(f"/chat/conversations/{conversation_id}")).json()["messages"][1]
    url = f"/chat/conversations/{conversation_id}/messages"

    # An answer, or an id that isn't in the conversation, isn't a question.
    assert (await client.delete(f"{url}/{answer['id']}")).status_code == 404
    assert (await client.delete(f"{url}/{uuid.uuid4()}")).status_code == 404

    # While a reply is being written, the turn holds the lock.
    async def locked(*_: Any) -> bool:
        return False

    with monkeypatch.context() as patched:
        patched.setattr(chat_service, "_lock", locked)
        assert (await client.delete(f"{url}/{question}")).status_code == 409

    # Someone else can't delete it.
    other = await make_user(db, "other@example.com")
    await sign_in_as(db, client, other)
    assert (await client.delete(f"{url}/{question}")).status_code == 404
