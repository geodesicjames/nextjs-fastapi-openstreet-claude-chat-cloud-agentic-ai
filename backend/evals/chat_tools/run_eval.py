"""Run the chat tool-use eval: real model, real tools, frozen map data.

    cd backend
    uv run python -m evals.chat_tools.run_eval --approve-harness   # once, after reviewing changes
    uv run python -m evals.chat_tools.run_eval --cases s01,d01      # a few cases
    uv run python -m evals.chat_tools.run_eval --reps 2             # the full set

Each run:
- recreates a throwaway database (<your DB name>_eval) and loads
  fixture.json.gz into it, so every run sees identical map data
  (see evals/common.py, shared with the trip planner eval);
- calls the app's real entry point, `chat.stream_reply`, with the real
  Claude model and the real tools. Only the external services are stubbed:
  Overpass (the fixture's cells are fresh; anything outside them reports
  "data may be missing") and Nominatim (a gazetteer of known places);
- grades each case (grade.py) and appends a row to
  .claude/hillclimb/chat-tools/<variant>/results.jsonl as it completes,
  plus the full conversation in traces/. Failed attempts that produced no
  answer (API errors, timeouts, a different model answering) go to
  errors.jsonl instead, so plumbing is never scored as a model failure.

Re-running a variant resumes: (case, rep) pairs already in results.jsonl
are skipped. Paid calls only happen when you run it: the Claude model under
test, plus the judge for the `grounded` metric.

The harness gate: the runner refuses to start if its own files (runner,
grader, cases, fixture) changed since you last approved them with
--approve-harness, so a score can't silently come from a different eval.
"""

import argparse
import asyncio
import json
import random
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

# First: points the app at the eval database before app.db is imported.
from evals.common import (
    HILLCLIMB,
    FixtureOverpass,
    GazetteerNominatim,
    Harness,
    append_row,
    done_keys,
    gazetteer_coords,
    get_settings,
    load_fixture,
    load_fixture_file,
    migrate,
    read_rows,
    recreate_database,
    wilson,
)

# isort: split
import anthropic  # noqa: E402
import httpx  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.models import ChatGrant, Place, User  # noqa: E402
from app.services import chat as chat_service  # noqa: E402
from app.services.chat_access import get_chat_status  # noqa: E402
from app.services.chat_tools import ToolContext  # noqa: E402
from app.services.llm import AnthropicChatModel, ModelReply  # noqa: E402
from app.services.pricing import PRICES, TokenUsage, cost_usd  # noqa: E402
from evals.chat_tools.grade import ToolCall, grade_tool_use, judge_grounded  # noqa: E402

HERE = Path(__file__).resolve().parent
FLOW_DIR = HILLCLIMB / "chat-tools"
HARNESS = Harness(
    FLOW_DIR,
    [
        HERE / "run_eval.py",
        HERE / "grade.py",
        HERE / "cases.json",
        HERE / "fixture.json.gz",
        HERE.parent / "common.py",
    ],
    {
        "metrics": [
            {"id": "tool_use", "kind": "binary", "label": "Tool use"},
            {"id": "grounded", "kind": "binary", "label": "Grounded"},
        ],
        "perf_fields": [
            {"id": "latency_s", "label": "Latency", "unit": "s"},
            {"id": "tool_calls", "label": "Tool calls"},
            {"id": "cost_usd", "label": "Cost", "unit": "$"},
        ],
    },
)


# --- The model, recorded ----------------------------------------------------------------------


class RecordingModel:
    """Wraps the real model and keeps every call's request and reply."""

    def __init__(self, inner: AnthropicChatModel) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    async def stream(self, **kwargs: Any):
        reply: ModelReply | None = None
        async for item in self.inner.stream(**kwargs):
            if isinstance(item, ModelReply):
                reply = item
            yield item
        self.calls.append({"messages": kwargs["messages"], "reply": reply})


# --- One case --------------------------------------------------------------------------------


class AttemptError(Exception):
    def __init__(
        self, failure_class: str, detail: str, usage: dict[str, Any] | None = None
    ) -> None:
        super().__init__(detail)
        self.failure_class = failure_class
        self.detail = detail
        self.usage = usage


def _sum_usage(replies: list[ModelReply]) -> dict[str, int]:
    return {
        "input_tokens": sum(r.usage.input_tokens for r in replies),
        "output_tokens": sum(r.usage.output_tokens for r in replies),
        "cache_read_input_tokens": sum(r.usage.cache_read_input_tokens for r in replies),
        "cache_creation_input_tokens": sum(r.usage.cache_creation_input_tokens for r in replies),
    }


def _trace(messages: list[Any], final: ModelReply | None) -> list[dict[str, Any]]:
    """The conversation in the report's trace format."""
    turns: list[dict[str, Any]] = [{"role": "system", "content": chat_service.SYSTEM_PROMPT}]
    blocks = [(m["role"], b) for m in messages for b in m["content"]]
    blocks += [("assistant", b) for b in (final.content if final else [])]
    for role, b in blocks:
        kind = b.get("type")
        if kind == "text":
            turns.append({"role": role, "content": b["text"]})
        elif kind == "tool_use":
            turns.append(
                {
                    "role": "tool_call",
                    "name": b["name"],
                    "content": json.dumps(b["input"], indent=2, ensure_ascii=False),
                }
            )
        elif kind == "tool_result":
            turns.append({"role": "tool_result", "content": b["content"]})
    return turns


async def run_case(
    case: dict[str, Any],
    model_id: str,
    client: anthropic.AsyncAnthropic,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    """Run one case through the real entry point; return everything needed to grade it."""
    settings = get_settings().model_copy(
        update={"chat_model": model_id, "chat_daily_budget_usd": Decimal(100)}
    )
    recorder = RecordingModel(AnthropicChatModel(client))
    async with SessionLocal() as db:
        user = User(email=f"eval-{uuid.uuid4().hex[:12]}@example.com")
        db.add(user)
        await db.flush()
        db.add(ChatGrant(user_id=user.id, note="eval"))
        await db.commit()
        access = await get_chat_status(db, settings, user)
        conversation = await chat_service.create_conversation(db, user)
        tools = ToolContext(
            db=db,
            settings=settings,
            user=user,
            overpass=FixtureOverpass(http=httpx.AsyncClient()),
            nominatim=GazetteerNominatim(http=httpx.AsyncClient(), gazetteer=fixture["gazetteer"]),
        )
        started = time.monotonic()
        answer, errors = "", []
        async for event in chat_service.stream_reply(
            db, settings, recorder, access, conversation, case["prompt"], tools
        ):
            if isinstance(event, chat_service.TextEvent):
                answer += event.text
            elif isinstance(event, chat_service.ErrorEvent):
                errors.append(event.code)
        latency = time.monotonic() - started

        replies = [c["reply"] for c in recorder.calls if c["reply"] is not None]
        usage = _sum_usage(replies)
        if "unavailable" in errors:
            raise AttemptError(
                "serving_error", "the model API was unavailable", usage if replies else None
            )
        for r in replies:
            if r.served_model != model_id:
                raise AttemptError(
                    "served_model_mismatch",
                    f"asked for {model_id}, {r.served_model} answered",
                    usage,
                )

        # Tool calls with the results that were sent back for them.
        results_by_id: dict[str, dict[str, Any]] = {}
        for call in recorder.calls:
            for m in call["messages"]:
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        results_by_id[b["tool_use_id"]] = b
        tool_calls = [
            ToolCall(
                name=b["name"],
                input=b["input"],
                result=(results_by_id.get(b["id"]) or {}).get("content"),
                is_error=bool((results_by_id.get(b["id"]) or {}).get("is_error")),
            )
            for r in replies
            for b in r.content
            if b.get("type") == "tool_use"
        ]
        place_ids = [str(c.input.get("place_id")) for c in tool_calls if c.input.get("place_id")]
        place_names: dict[str, str | None] = {}
        for pid in place_ids:
            try:
                place = await db.get(Place, uuid.UUID(pid))
            except ValueError:
                place = None
            place_names[pid] = place.name if place else None

    last = recorder.calls[-1] if recorder.calls else {"messages": [], "reply": None}
    return {
        "answer": answer,
        "errors": errors,
        "latency_s": round(latency, 2),
        "replies": replies,
        "usage": usage,
        "tool_calls": tool_calls,
        "place_names": place_names,
        "trace": _trace(last["messages"], last["reply"]),
    }


# --- The run -----------------------------------------------------------------------------------


def row_cost(row: dict[str, Any]) -> float:
    """Derived when reporting (not stored): model + judge, from usage and prices."""

    def one(model: str | None, usage: dict[str, int] | None) -> Decimal:
        if not model or not usage or model not in PRICES:
            return Decimal(0)
        return cost_usd(model, TokenUsage(**usage))

    return float(
        one(row.get("model"), row.get("usage"))
        + one(row.get("judge_model"), row.get("judge_usage"))
    )


async def run(args: argparse.Namespace) -> int:
    cases = json.loads((HERE / "cases.json").read_text())
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    fixture = load_fixture_file()
    gazetteer = gazetteer_coords(fixture)

    # "The Meadows" / "Arthur's Seat" as written in cases: map to the gazetteer.
    def coords(name: str) -> tuple[float, float]:
        key = name.lower()
        return next(v for alias, v in gazetteer.items() if alias in key)

    near = {
        c["expect"]["near"].lower(): coords(c["expect"]["near"])
        for c in cases
        if "near" in c["expect"]
    }

    variant_dir = FLOW_DIR / args.variant
    results_path, errors_path = variant_dir / "results.jsonl", variant_dir / "errors.jsonl"
    done = done_keys(results_path)
    todo = [(c, rep) for c in cases for rep in range(args.reps) if (c["id"], rep) not in done]
    print(
        f"{len(todo)} attempts to run ({len(done)} already done) "
        f"on {args.model}, judge {args.judge_model}"
    )

    key = get_settings().anthropic_api_key
    if key is None:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2
    client = anthropic.AsyncAnthropic(api_key=key.get_secret_value())
    semaphore = asyncio.Semaphore(args.concurrency)

    async def attempt(case: dict[str, Any], rep: int) -> None:
        async with semaphore:
            result = None
            attempt_no = 0
            for attempt_no in range(1, args.attempts + 1):
                try:
                    outcome = await asyncio.wait_for(
                        run_case(case, args.model, client, fixture), args.timeout_s
                    )
                    tool_grade = grade_tool_use(
                        case["expect"], outcome["tool_calls"], near, outcome["place_names"]
                    )
                    verdict = await judge_grounded(
                        client,
                        args.judge_model,
                        case["prompt"],
                        outcome["tool_calls"],
                        outcome["answer"],
                    )
                    result = (outcome, tool_grade, verdict)
                    break
                except (TimeoutError, AttemptError, anthropic.APIError) as error:
                    failure = (
                        "timeout"
                        if isinstance(error, TimeoutError)
                        else error.failure_class
                        if isinstance(error, AttemptError)
                        else "judge_error"
                    )
                    append_row(
                        errors_path,
                        {
                            "prompt_id": case["id"],
                            "rep": rep,
                            "attempt": attempt_no,
                            "failure_class": failure,
                            "detail": str(error)[:500],
                            "usage": getattr(error, "usage", None),
                            "model": args.model,
                        },
                    )
                    if attempt_no == args.attempts:
                        print(f"  {case['id']} rep {rep}: FAILED ({failure})")
                    else:
                        await asyncio.sleep(random.uniform(5, 15) * attempt_no)  # jittered backoff
            if result is None:
                return  # every attempt failed; recorded in errors.jsonl
            outcome, tool_grade, verdict = result

            replies: list[ModelReply] = outcome["replies"]
            stop_reasons = [r.stop_reason for r in replies]
            refused = "refused" in outcome["errors"]
            truncated = "max_tokens" in stop_reasons or "truncated" in outcome["errors"]
            trace_name = f"{case['id']}_rep{rep}.json"
            (variant_dir / "traces").mkdir(parents=True, exist_ok=True)
            (variant_dir / "traces" / trace_name).write_text(
                json.dumps(outcome["trace"], indent=1, ensure_ascii=False)
            )
            row = {
                "prompt_id": case["id"],
                "rep": rep,
                "prompt": case["prompt"],
                "tags": case["tags"],
                "grade": {"tool_use": int(tool_grade.passed), "grounded": int(verdict.grounded)},
                "explanation": {
                    "tool_use": "; ".join(tool_grade.failures) or "all checks passed",
                    "grounded": verdict.explanation,
                },
                "status": "truncated" if truncated else "ok",
                "stop_reason": stop_reasons[-1] if stop_reasons else None,
                "model": replies[-1].served_model if replies else args.model,
                "usage": outcome["usage"],
                "judge_model": verdict.model,
                "judge_usage": verdict.usage,
                "latency_s": outcome["latency_s"],
                "tool_calls": len(outcome["tool_calls"]),
                "attempts": attempt_no,
                "meta": {
                    "refused": refused,
                    "model_calls": len(replies),
                    "answer": outcome["answer"],
                    "tools": [
                        {"name": c.name, "input": c.input, "is_error": c.is_error}
                        for c in outcome["tool_calls"]
                    ],
                    "judge": verdict.detail,
                    "trace": f"{args.variant}/traces/{trace_name}",
                },
            }
            row["cost_usd"] = round(row_cost(row), 6)
            append_row(results_path, row)
            marks = "".join("✓" if v else "✗" for v in row["grade"].values())
            print(
                f"  {case['id']} rep {rep}: {marks}  "
                f"{row['latency_s']:5.1f}s  ${row['cost_usd']:.5f}"
            )

    started = time.monotonic()
    await asyncio.gather(*(attempt(c, rep) for c, rep in todo))
    await client.close()
    await engine.dispose()
    print(f"Ran in {time.monotonic() - started:.0f}s")

    # Summary from the raw rows (all reps of the requested cases, including resumed ones).
    ids = {c["id"] for c in cases}
    rows = [r for r in read_rows(results_path) if r["prompt_id"] in ids]
    scored = [r for r in rows if r["status"] == "ok"]
    print(
        f"\n{len(rows)} rows ({len(rows) - len(scored)} truncated, excluded); "
        f"errors file: {errors_path}"
    )
    for metric in ("tool_use", "grounded"):
        passes = sum(r["grade"][metric] for r in scored)
        lo, hi = wilson(passes, len(scored))
        print(
            f"  {metric:9} {passes}/{len(scored)} = {passes / max(len(scored), 1):.0%}  "
            f"(95% CI {lo:.0%}-{hi:.0%})"
        )
    if rows:
        lat = sorted(r["latency_s"] for r in rows)
        print(f"  latency   median {lat[len(lat) // 2]:.1f}s, max {lat[-1]:.1f}s")
        print(f"  tools     mean {sum(r['tool_calls'] for r in rows) / len(rows):.1f} calls")
        total = sum(row_cost(r) for r in rows)
        print(f"  cost      ${total:.4f} total (model + judge), ${total / len(rows):.5f}/case")
    return 0


def calls_from_trace(trace: list[dict[str, Any]]) -> list[ToolCall]:
    """Rebuild tool calls and their results from a saved trace.

    Results come back in the same order as the calls that asked for them, so
    each tool_result answers the oldest call still waiting for one.
    """
    calls: list[ToolCall] = []
    waiting: list[ToolCall] = []
    for turn in trace:
        if turn["role"] == "tool_call":
            call = ToolCall(name=turn["name"], input=json.loads(turn["content"]), result=None)
            calls.append(call)
            waiting.append(call)
        elif turn["role"] == "tool_result" and waiting:
            call = waiting.pop(0)
            call.result = turn["content"]
            try:
                call.is_error = "error" in json.loads(turn["content"])
            except (ValueError, TypeError):
                call.is_error = False
    return calls


async def regrade(args: argparse.Namespace) -> int:
    """Re-judge `grounded` for stored answers, without calling the app's model.

    Keeps the answers, tool calls and tool_use grades; replaces the judge's
    verdict. The previous file is kept as results.before-regrade.jsonl.
    """
    variant_dir = FLOW_DIR / args.variant
    results_path = variant_dir / "results.jsonl"
    rows = read_rows(results_path)
    key = get_settings().anthropic_api_key
    if key is None:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2
    client = anthropic.AsyncAnthropic(api_key=key.get_secret_value())
    semaphore = asyncio.Semaphore(args.concurrency)
    print(f"Re-grading {len(rows)} rows with {args.judge_model} (judge only)")

    async def one(row: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            trace = json.loads((FLOW_DIR / row["meta"]["trace"]).read_text())
            verdict = await judge_grounded(
                client,
                args.judge_model,
                row["prompt"],
                calls_from_trace(trace),
                row["meta"]["answer"],
            )
        before = row["grade"]["grounded"]
        row["grade"]["grounded"] = int(verdict.grounded)
        row["explanation"]["grounded"] = verdict.explanation
        row["judge_model"], row["judge_usage"] = verdict.model, verdict.usage
        row["meta"]["judge"] = verdict.detail
        row["cost_usd"] = round(row_cost(row), 6)
        if before != row["grade"]["grounded"]:
            change = f"{before} -> {row['grade']['grounded']}"
            print(f"  {row['prompt_id']} rep {row['rep']}: grounded {change}")
        return row

    new_rows = await asyncio.gather(*(one(r) for r in rows))
    await client.close()
    (variant_dir / "results.before-regrade.jsonl").write_text(results_path.read_text())
    results_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in new_rows)
    )
    scored = [r for r in new_rows if r["status"] == "ok"]
    passes = sum(r["grade"]["grounded"] for r in scored)
    lo, hi = wilson(passes, len(scored))
    judge_cost = sum(
        float(cost_usd(r["judge_model"], TokenUsage(**r["judge_usage"]))) for r in new_rows
    )
    rate = passes / max(len(scored), 1)
    print(f"  grounded  {passes}/{len(scored)} = {rate:.0%}  (95% CI {lo:.0%}-{hi:.0%})")
    print(f"  judge cost of this re-grade: ${judge_cost:.4f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--variant", default="baseline", help="baseline or v1, v2, ...")
    parser.add_argument("--model", default=get_settings().chat_model)
    parser.add_argument("--judge-model", default="claude-sonnet-5-5")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout-s", type=float, default=180)
    parser.add_argument(
        "--attempts", type=int, default=2, help="tries per case on API errors/timeouts"
    )
    parser.add_argument(
        "--regrade",
        action="store_true",
        help="re-run only the grounded judge on the variant's stored answers (no app model calls)",
    )
    parser.add_argument(
        "--approve-harness",
        action="store_true",
        help="record the current runner/grader/cases/fixture as approved, then exit",
    )
    args = parser.parse_args()

    if args.approve_harness:
        print(HARNESS.approve())
        return 0
    if not HARNESS.approved():
        print(
            "The eval's runner, grader, cases or fixture changed since they were last approved.\n"
            "Review the changes, then run:\n"
            "  uv run python -m evals.chat_tools.run_eval --approve-harness",
            file=sys.stderr,
        )
        return 2
    if args.model not in PRICES:
        print(f"No prices for {args.model}; add it to app/services/pricing.py", file=sys.stderr)
        return 2

    if args.regrade:
        return asyncio.run(regrade(args))

    # Fresh database with the fixture loaded. Migrations run outside the event
    # loop (alembic's env.py starts its own).
    asyncio.run(recreate_database())
    migrate()
    fixture = load_fixture_file()

    async def go() -> int:
        await load_fixture(fixture)
        return await run(args)

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
