"""The agent loop (app/services/agent.py), driven by a scripted fake model.

The task here is a toy: the agent can `lookup` a number and must submit 42
with `submit_answer`. The check fails any other answer, so tests can walk
through plan -> act -> check -> revise -> approval without trip-planner
details (those come with the trip task and its own tests).
"""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import AgentRun, AgentStep, LlmUsage, RunStatus, StepKind, User
from app.services import agent
from app.services.agent import AgentTask, CheckResult
from app.services.chat_tools import ToolContext, ToolOutcome
from app.services.llm import ModelUnavailableError
from app.services.pricing import TokenUsage
from tests.chat_helpers import THINKING, FakeChatModel, reply
from tests.factories import grant, make_user


async def run_toy_tool(ctx: ToolContext, name: str, raw_input: object) -> ToolOutcome:
    if name == "explode":
        raise RuntimeError("bug in a tool")
    return ToolOutcome(json.dumps({"number": 42}), summary="Looked it up")


async def check_toy(
    ctx: ToolContext, run: AgentRun, steps: list[AgentStep], proposal: object
) -> CheckResult:
    answer = proposal.get("answer") if isinstance(proposal, dict) else None
    if answer != 42:
        return CheckResult(ok=False, problems=[f"{answer} is not the number lookup returned."])
    return CheckResult(ok=True)


TOY = AgentTask(
    name="toy",
    system="Find the number and submit it.",
    tools=[
        {
            "name": "lookup",
            "description": "Look up the number.",
            "input_schema": {"type": "object"},
        },
        {
            "name": "submit_answer",
            "description": "Submit the answer.",
            "input_schema": {"type": "object", "properties": {"answer": {"type": "integer"}}},
        },
    ],
    finish_tool="submit_answer",
    run_tool=run_toy_tool,
    check=check_toy,
)


def call(
    name: str,
    tool_input: dict[str, Any] | None = None,
    *,
    id: str,
    usage: TokenUsage | None = None,
) -> list[Any]:
    """A scripted model reply that calls one tool."""
    block = {"type": "tool_use", "id": id, "name": name, "input": tool_input or {}}
    return reply(content=[THINKING, block], stop_reason="tool_use", usage=usage)


def settings(**changes: object) -> Settings:
    return get_settings().model_copy(update=changes)


@pytest.fixture
async def user(db: AsyncSession) -> User:
    user = await make_user(db, "planner@example.com")
    await grant(db, user)
    return user


def context(db: AsyncSession, user: User, s: Settings) -> ToolContext:
    return ToolContext(
        db=db, settings=s, user=user, overpass=cast(Any, None), nominatim=cast(Any, None)
    )


async def advance(
    db: AsyncSession, model: FakeChatModel, run: AgentRun, user: User, s: Settings | None = None
) -> list[AgentStep]:
    s = s or settings()
    return [step async for step in agent.advance(db, s, model, TOY, run, context(db, user, s))]


async def new_run(db: AsyncSession, user: User, goal: str = "What's the number?") -> AgentRun:
    return await agent.create_run(db, settings(), user, TOY, goal)


async def test_plan_act_check_revise_then_approve(db: AsyncSession, user: User) -> None:
    model = FakeChatModel(
        [
            call("lookup", id="t1"),
            call("submit_answer", {"answer": 41}, id="t2"),  # fails the check
            call("submit_answer", {"answer": 42}, id="t3"),
        ]
    )
    run = await new_run(db, user)
    steps = await advance(db, model, run, user)

    assert [s.kind for s in steps] == ["model", "tool", "model", "check", "model", "check"]
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.plan == {"answer": 42}
    assert run.model_calls == 3

    # The failed check went back to the model as the finish tool's result.
    third_call = model.calls[2]["messages"]
    assert third_call[-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "t2",
                "content": steps[3].data["result"],
                "is_error": True,
            }
        ],
    }
    assert "41 is not the number" in steps[3].data["result"]

    # Every call is in the usage ledger, tagged with the run, and summed on it.
    usage = list(await db.scalars(select(LlmUsage).where(LlmUsage.agent_run_id == run.id)))
    assert len(usage) == 3
    assert run.cost_usd == sum((u.cost_usd for u in usage), Decimal(0)) > 0
    assert model.calls[0]["max_tokens"] == get_settings().agent_max_tokens
    assert model.calls[0]["system"] == TOY.system

    assert await agent.approve(db, run)
    assert run.status == RunStatus.DONE and run.finished_at is not None
    assert not await agent.approve(db, run)  # only once


async def test_requested_changes_go_back_to_the_model(db: AsyncSession, user: User) -> None:
    model = FakeChatModel(
        [
            call("submit_answer", {"answer": 42}, id="t1"),
            call("submit_answer", {"answer": 42}, id="t2"),
        ]
    )
    run = await new_run(db, user)
    await advance(db, model, run, user)
    assert run.status == RunStatus.AWAITING_APPROVAL

    assert await agent.request_changes(db, run, "Explain it as well, please.", TOY)
    assert run.status == RunStatus.QUEUED
    steps = await advance(db, model, run, user)

    assert [s.kind for s in steps] == ["model", "check"]
    assert run.status == RunStatus.AWAITING_APPROVAL
    feedback = model.calls[1]["messages"][-1]["content"][0]
    assert feedback["tool_use_id"] == "t1" and "is_error" not in feedback
    assert json.loads(feedback["content"])["feedback_from_user"] == "Explain it as well, please."
    # The replayed history is the first call's, plus what came after: append-only.
    assert (
        model.calls[1]["messages"][: len(model.calls[0]["messages"])] == model.calls[0]["messages"]
    )


async def test_last_call_is_told_to_submit_then_the_run_stops(db: AsyncSession, user: User) -> None:
    s = settings(agent_max_model_calls=3)
    model = FakeChatModel([call("lookup", id=f"t{i}") for i in range(3)])
    run = await new_run(db, user)
    steps = await advance(db, model, run, user, s)

    nudges = [st for st in steps if st.kind == StepKind.NUDGE]
    assert [n.data["reason"] for n in nudges] == ["last_call"]
    last_message = model.calls[2]["messages"][-1]["content"]
    assert last_message[-1] == {"type": "text", "text": nudges[0].data["text"]}
    assert last_message[0]["type"] == "tool_result"  # tool results first

    assert run.status == RunStatus.FAILED and run.error == "step_limit"
    assert len(model.calls) == 3


async def test_a_last_proposal_that_fails_the_check_ends_the_run(
    db: AsyncSession, user: User
) -> None:
    s = settings(agent_max_model_calls=2)
    model = FakeChatModel([call("lookup", id="t1"), call("submit_answer", {"answer": 7}, id="t2")])
    run = await new_run(db, user)
    await advance(db, model, run, user, s)
    assert run.status == RunStatus.FAILED and run.error == "check_failed"


async def test_cost_limit(db: AsyncSession, user: User) -> None:
    model = FakeChatModel([call("lookup", id="t1")])
    run = await new_run(db, user)
    await advance(db, model, run, user, settings(agent_max_cost_usd=Decimal("0.000001")))
    assert run.status == RunStatus.FAILED and run.error == "cost_limit"
    assert len(model.calls) == 1


async def test_time_limit(db: AsyncSession, user: User) -> None:
    model = FakeChatModel([call("lookup", id="t1")])
    run = await new_run(db, user)
    await advance(db, model, run, user, settings(agent_max_seconds=0))
    assert run.status == RunStatus.FAILED and run.error == "time_limit"


async def test_daily_budget_applies(db: AsyncSession) -> None:
    user = await make_user(db, "thrifty@example.com")
    await grant(db, user, daily_budget_usd=Decimal("0.01"))
    # Haiku 5.5: 50K input + 20K output tokens is $0.015, over the budget.
    expensive = TokenUsage(input_tokens=50_000, output_tokens=20_000)
    model = FakeChatModel([call("lookup", id="t1", usage=expensive), call("lookup", id="t2")])
    run = await new_run(db, user)
    await advance(db, model, run, user)
    assert run.status == RunStatus.FAILED and run.error == "budget"
    assert len(model.calls) == 1  # the first call was under budget


async def test_no_grant_means_no_model_calls(db: AsyncSession) -> None:
    user = await make_user(db, "stranger@example.com")
    model = FakeChatModel()
    run = await new_run(db, user)
    await advance(db, model, run, user)
    assert run.status == RunStatus.FAILED and run.error == "no_access"
    assert model.calls == []


@pytest.mark.parametrize(
    ("script", "error"),
    [
        (ModelUnavailableError("api_error"), "unavailable"),
        (reply("I can't help with that.", stop_reason="refusal"), "refused"),
        (
            reply(
                content=[{"type": "tool_use", "id": "t1", "name": "lookup", "input": {}}],
                stop_reason="max_tokens",
            ),
            "truncated",
        ),
    ],
)
async def test_model_failures_end_the_run(
    db: AsyncSession, user: User, script: Any, error: str
) -> None:
    run = await new_run(db, user)
    await advance(db, FakeChatModel([script]), run, user)
    assert run.status == RunStatus.FAILED and run.error == error


async def test_a_text_answer_is_nudged_to_keep_going(db: AsyncSession, user: User) -> None:
    model = FakeChatModel(
        [reply("Which number do you mean?"), call("submit_answer", {"answer": 42}, id="t1")]
    )
    run = await new_run(db, user)
    steps = await advance(db, model, run, user)
    assert [s.kind for s in steps] == ["model", "nudge", "model", "check"]
    assert steps[1].data["reason"] == "keep_going"
    assert run.status == RunStatus.AWAITING_APPROVAL


async def test_a_failing_tool_is_reported_not_fatal(db: AsyncSession, user: User) -> None:
    model = FakeChatModel(
        [call("explode", id="t1"), call("submit_answer", {"answer": 42}, id="t2")]
    )
    run = await new_run(db, user)
    steps = await advance(db, model, run, user)
    assert steps[1].data["is_error"] is True
    assert run.status == RunStatus.AWAITING_APPROVAL


async def test_cancel_stops_a_run_at_its_next_step(db: AsyncSession, user: User) -> None:
    model = FakeChatModel([call("lookup", id="t1"), call("lookup", id="t2")])
    run = await new_run(db, user)
    steps: list[AgentStep] = []
    async for step in agent.advance(db, settings(), model, TOY, run, context(db, user, settings())):
        steps.append(step)
        if step.kind == StepKind.MODEL:
            # As if the user pressed Cancel while the tool was about to run.
            assert await agent.cancel(db, run)
    assert run.status == RunStatus.CANCELLED
    assert [s.kind for s in steps] == ["model"]
    assert len(model.calls) == 1


async def test_an_interrupted_run_resumes_from_its_last_step(db: AsyncSession, user: User) -> None:
    # A first worker saved a model step asking for a tool, then died.
    model = FakeChatModel([call("lookup", id="t1")])
    run = await new_run(db, user)
    gen = agent.advance(db, settings(), model, TOY, run, context(db, user, settings()))
    first = await anext(gen)
    assert first.kind == StepKind.MODEL
    await gen.aclose()
    assert run.status == RunStatus.RUNNING

    # While its heartbeat is recent, nobody else may take the run over.
    model.replies.append(call("submit_answer", {"answer": 42}, id="t2"))
    assert await advance(db, model, run, user) == []

    # Once it is stale, the next worker runs the missing tool and carries on.
    run.heartbeat_at = run.heartbeat_at - timedelta(minutes=10)  # type: ignore[operator]
    await db.commit()
    steps = await advance(db, model, run, user)
    assert [s.kind for s in steps] == ["tool", "model", "check"]
    assert run.status == RunStatus.AWAITING_APPROVAL
    # The resumed call saw the original model step and its tool result.
    assert model.calls[1]["messages"][1]["content"] == first.data["content"]


async def test_finished_or_waiting_runs_are_not_advanced(db: AsyncSession, user: User) -> None:
    model = FakeChatModel([call("submit_answer", {"answer": 42}, id="t1")])
    run = await new_run(db, user)
    await advance(db, model, run, user)
    assert await advance(db, model, run, user) == []  # awaiting approval
    assert await agent.cancel(db, run)
    assert await advance(db, model, run, user) == []
    assert not await agent.cancel(db, run)  # already finished
    assert not await agent.request_changes(db, run, "more", TOY)


async def test_runs_are_private(db: AsyncSession, user: User) -> None:
    run = await new_run(db, user)
    other = await make_user(db, "other@example.com")
    assert await agent.get_run(db, user, run.id) is run
    assert await agent.get_run(db, other, run.id) is None
