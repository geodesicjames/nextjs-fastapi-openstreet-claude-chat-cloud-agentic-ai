"""Run the trip-planner eval: real model, real agent loop, frozen map data.

    cd backend
    uv run python -m evals.trip_planner.run_eval --dry-run           # free: a scripted model
    uv run python -m evals.trip_planner.run_eval --approve-harness   # once, after reviewing changes
    uv run python -m evals.trip_planner.run_eval --cases t01,t04     # a few cases
    uv run python -m evals.trip_planner.run_eval --reps 2            # the full set
    uv run python -m evals.trip_planner.run_eval --variant sonnet --model claude-sonnet-5-5
    uv run python -m evals.trip_planner.run_eval --report            # compare variants

Each case is a trip goal (cases.json). The runner:
- recreates the throwaway eval database and loads the chat eval's map
  fixture (central Edinburgh), with Overpass and Nominatim stubbed out
  (evals/common.py);
- runs the goal through the worker's own entry point,
  `agent_worker.drive_run`, so the real agent loop, tools, check and
  guardrails are all measured; only the model differs between variants;
- grades the result in code (grade.py): did a plan pass the check, and does
  it match what the goal asked for? There's no judge model, so the only
  cost is the model under test;
- appends a row per (case, rep) to
  .claude/hillclimb/trip-planner/<variant>/results.jsonl, with the run's
  full trace in traces/. API failures go to errors.jsonl instead.

Re-running a variant resumes where it left off. `--dry-run` swaps in a
scripted model that plans from the case's expectations: it costs nothing,
writes no results, and checks the plumbing and that every case can be
satisfied by the fixture's data.

The harness gate: the runner refuses paid runs if its own files (runner,
grader, cases, shared plumbing, fixture) changed since you last approved
them with --approve-harness.
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

# First: points the app at the eval database before app.db is imported.
from evals.common import (
    FIXTURE,
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
from app.models import AgentRun, ChatGrant, StepKind, User  # noqa: E402
from app.schemas.agent import TraceStepOut  # noqa: E402
from app.services import agent  # noqa: E402
from app.services.agent_worker import drive_run  # noqa: E402
from app.services.llm import AnthropicChatModel, ChatModel, ModelReply  # noqa: E402
from app.services.pricing import PRICES, TokenUsage  # noqa: E402
from app.services.trip_planner import TRIP_PLANNER  # noqa: E402
from evals.trip_planner.grade import TripGrade, coords_for, grade_run  # noqa: E402

HERE = Path(__file__).resolve().parent
FLOW_DIR = HILLCLIMB / "trip-planner"
HARNESS = Harness(
    FLOW_DIR,
    [
        HERE / "run_eval.py",
        HERE / "grade.py",
        HERE / "cases.json",
        HERE.parent / "common.py",
        FIXTURE,
    ],
    {
        "metrics": [
            {"id": "success", "kind": "binary", "label": "Plan meets the goal"},
            {"id": "first_try", "kind": "binary", "label": "First proposal valid"},
        ],
        "perf_fields": [
            {"id": "latency_s", "label": "Latency", "unit": "s"},
            {"id": "model_calls", "label": "Model calls"},
            {"id": "cost_usd", "label": "Cost", "unit": "$"},
        ],
    },
)
METRICS = ("success", "first_try")


# --- Models ----------------------------------------------------------------------------------


class RecordingModel:
    """Wraps a model and keeps every reply, to check which model actually answered."""

    def __init__(self, inner: ChatModel) -> None:
        self.inner = inner
        self.replies: list[ModelReply] = []

    async def stream(self, **kwargs: Any):
        async for item in self.inner.stream(**kwargs):
            if isinstance(item, ModelReply):
                self.replies.append(item)
            yield item


def _tool_results(messages: list[Any]) -> list[Any]:
    """The JSON outputs of the tools answered in the latest user message."""
    out = []
    for block in messages[-1]["content"]:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            content = block["content"]
            text = content if isinstance(content, str) else "".join(c["text"] for c in content)
            out.append(json.loads(text))
    return out


class ScriptedPlanner:
    """For --dry-run: plans from the case's expectations instead of calling Claude.

    Finds the start, searches for each expected category in turn, then proposes the
    nearest place of each (more cafés if the case wants several stops). It
    shows the plumbing works and the fixture can satisfy each case; it is not
    a baseline.
    """

    def __init__(self, case: dict[str, Any], model_id: str) -> None:
        self.expect = case["expect"]
        self.model_id = model_id
        self.calls = 0
        self.start: dict[str, Any] = {}
        self.found: list[dict[str, Any]] = []

    async def stream(self, **kwargs: Any):
        self.calls += 1
        e = self.expect
        categories: list[str] = e["categories"]
        results = _tool_results(kwargs["messages"]) if self.calls > 1 else []
        if self.calls == 2:
            match = results[0]["matches"][0]
            self.start = {"name": e["near"], "lat": match["lat"], "lon": match["lon"]}
        elif self.calls > 2:
            self.found += results[0].get("places", [])

        if self.calls == 1:
            name, tool_input = "find_location", {"query": f"{e['near']}, Edinburgh"}
        elif self.calls - 2 < len(categories):
            # One search per category, so plentiful ones don't crowd out the rest.
            name = "search_places"
            tool_input = {
                "lat": self.start["lat"],
                "lon": self.start["lon"],
                "name": None,
                "radius_m": 1500,
                "categories": [categories[self.calls - 2]],
            }
        else:
            per_category = max(1, e.get("min_stops", 1) - len(categories) + 1)
            stops = [
                p
                for category in categories
                for p in [p for p in self.found if p["category"] == category][:per_category]
            ]
            name = "submit_plan"
            tool_input = {
                "title": "Scripted plan",
                "summary": "A dry-run plan.",
                "categories": categories,
                "start": self.start,
                "return_to_start": e.get("return_to_start", False),
                "start_time": e.get("start_time") or ("09:00" if "end_by" in e else None),
                "end_by": e.get("end_by"),
                "max_minutes": e.get("max_minutes"),
                "stops": [
                    {"place_id": p["place_id"], "minutes": 15, "why": "Nearest."} for p in stops
                ],
            }
        yield ModelReply(
            content=[
                {"type": "tool_use", "id": f"toolu_{self.calls}", "name": name, "input": tool_input}
            ],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=0, output_tokens=0),
            model=self.model_id,
            served_model=self.model_id,
        )


# --- One case --------------------------------------------------------------------------------


class AttemptError(Exception):
    def __init__(self, failure_class: str, detail: str) -> None:
        super().__init__(detail)
        self.failure_class = failure_class


async def run_case(
    case: dict[str, Any],
    model_id: str,
    model: ChatModel,
    fixture: dict[str, Any],
) -> dict[str, Any]:
    """Run one goal through the agent; return the run, its steps and timings."""
    settings = get_settings().model_copy(
        update={"agent_model": model_id, "chat_daily_budget_usd": Decimal(100)}
    )
    recorder = RecordingModel(model)
    async with SessionLocal() as db, httpx.AsyncClient() as http:
        user = User(email=f"eval-{uuid.uuid4().hex[:12]}@example.com")
        db.add(user)
        await db.flush()
        db.add(ChatGrant(user_id=user.id, note="eval"))
        await db.commit()
        run = await agent.create_run(db, settings, user, TRIP_PLANNER, case["goal"])
        started = time.monotonic()
        await drive_run(
            db,
            settings,
            recorder,
            run.id,
            FixtureOverpass(http=http),
            GazetteerNominatim(http=http, gazetteer=fixture["gazetteer"]),
        )
        latency = time.monotonic() - started
        run = await db.get(AgentRun, run.id, populate_existing=True)
        assert run is not None
        steps = await agent.load_steps(db, run)

    if run.error == "unavailable":
        raise AttemptError("serving_error", "the model API was unavailable")
    for reply in recorder.replies:
        if reply.served_model != model_id:
            raise AttemptError(
                "served_model_mismatch", f"asked for {model_id}, {reply.served_model} answered"
            )
    return {"run": run, "steps": steps, "replies": recorder.replies, "latency_s": latency}


def _usage(replies: list[ModelReply]) -> dict[str, int]:
    return {
        "input_tokens": sum(r.usage.input_tokens for r in replies),
        "output_tokens": sum(r.usage.output_tokens for r in replies),
        "cache_read_input_tokens": sum(r.usage.cache_read_input_tokens for r in replies),
        "cache_creation_input_tokens": sum(r.usage.cache_creation_input_tokens for r in replies),
    }


def grade_outcome(
    case: dict[str, Any], outcome: dict[str, Any], gazetteer: dict[str, tuple[float, float]]
) -> tuple[TripGrade, int]:
    run: AgentRun = outcome["run"]
    failed_checks = sum(
        1 for s in outcome["steps"] if s.kind == StepKind.CHECK and not s.data["ok"]
    )
    grade = grade_run(case["expect"], run.status, run.error, run.plan, failed_checks, gazetteer)
    return grade, failed_checks


# --- The run -----------------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    cases = json.loads((HERE / "cases.json").read_text())
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    fixture = load_fixture_file()
    gazetteer = gazetteer_coords(fixture)
    for case in cases:  # fail fast on a typo'd place name
        coords_for(case["expect"]["near"], gazetteer)

    if args.dry_run:
        return await dry_run(cases, args, fixture, gazetteer)

    variant_dir = FLOW_DIR / args.variant
    results_path, errors_path = variant_dir / "results.jsonl", variant_dir / "errors.jsonl"
    done = done_keys(results_path)
    todo = [(c, rep) for c in cases for rep in range(args.reps) if (c["id"], rep) not in done]
    print(f"{len(todo)} attempts to run ({len(done)} already done) on {args.model}")

    key = get_settings().anthropic_api_key
    if key is None:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2
    client = anthropic.AsyncAnthropic(api_key=key.get_secret_value())
    model = AnthropicChatModel(client)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def attempt(case: dict[str, Any], rep: int) -> None:
        async with semaphore:
            outcome = None
            attempt_no = 0
            for attempt_no in range(1, args.attempts + 1):
                try:
                    outcome = await asyncio.wait_for(
                        run_case(case, args.model, model, fixture), args.timeout_s
                    )
                    break
                except (TimeoutError, AttemptError) as error:
                    failure = "timeout" if isinstance(error, TimeoutError) else error.failure_class
                    append_row(
                        errors_path,
                        {
                            "prompt_id": case["id"],
                            "rep": rep,
                            "attempt": attempt_no,
                            "failure_class": failure,
                            "detail": str(error)[:500],
                            "model": args.model,
                        },
                    )
                    if attempt_no == args.attempts:
                        print(f"  {case['id']} rep {rep}: FAILED ({failure})")
                    else:
                        await asyncio.sleep(random.uniform(5, 15) * attempt_no)
            if outcome is None:
                return  # every attempt failed; recorded in errors.jsonl

            grade, failed_checks = grade_outcome(case, outcome, gazetteer)
            agent_run: AgentRun = outcome["run"]
            replies: list[ModelReply] = outcome["replies"]
            trace_name = f"{case['id']}_rep{rep}.json"
            (variant_dir / "traces").mkdir(parents=True, exist_ok=True)
            (variant_dir / "traces" / trace_name).write_text(
                json.dumps(
                    [TraceStepOut.from_step(s).model_dump(mode="json") for s in outcome["steps"]],
                    indent=1,
                    ensure_ascii=False,
                )
            )
            row = {
                "prompt_id": case["id"],
                "rep": rep,
                "prompt": case["goal"],
                "tags": case["tags"],
                "grade": {"success": int(grade.success), "first_try": int(grade.first_try)},
                "explanation": {
                    "success": "; ".join(grade.failures) or "plan passed and meets the goal",
                    "first_try": f"{failed_checks} failed check(s) before the plan passed",
                },
                # Every run is scored: running out of steps or budget is the
                # agent's failure, not the harness's.
                "status": "ok",
                "run_status": agent_run.status,
                "run_error": agent_run.error,
                "model": replies[-1].served_model if replies else args.model,
                "usage": _usage(replies),
                "cost_usd": round(float(agent_run.cost_usd), 6),
                "latency_s": round(outcome["latency_s"], 2),
                "model_calls": agent_run.model_calls,
                "attempts": attempt_no,
                "meta": {
                    "failed_checks": failed_checks,
                    "plan": agent_run.plan,
                    "trace": f"{args.variant}/traces/{trace_name}",
                },
            }
            append_row(results_path, row)
            marks = "".join("✓" if v else "✗" for v in row["grade"].values())
            print(
                f"  {case['id']} rep {rep}: {marks}  {row['model_calls']:2} calls  "
                f"{row['latency_s']:5.1f}s  ${row['cost_usd']:.4f}"
                + ("" if grade.success else f"  ({row['explanation']['success']})")
            )

    started = time.monotonic()
    await asyncio.gather(*(attempt(c, rep) for c, rep in todo))
    await client.close()
    await engine.dispose()
    print(f"Ran in {time.monotonic() - started:.0f}s\n")

    ids = {c["id"] for c in cases}
    print_summary(args.variant, [r for r in read_rows(results_path) if r["prompt_id"] in ids])
    print(f"Errors file: {errors_path}")
    return 0


async def dry_run(
    cases: list[dict[str, Any]],
    args: argparse.Namespace,
    fixture: dict[str, Any],
    gazetteer: dict[str, tuple[float, float]],
) -> int:
    """Every case with the scripted model: free, and nothing is written."""
    print(f"Dry run: {len(cases)} cases with a scripted model (no Claude calls, no results)")
    passed = 0
    for case in cases:
        outcome = await run_case(case, args.model, ScriptedPlanner(case, args.model), fixture)
        grade, _ = grade_outcome(case, outcome, gazetteer)
        passed += grade.success
        agent_run: AgentRun = outcome["run"]
        problems = [
            p for s in outcome["steps"] if s.kind == StepKind.CHECK for p in s.data["problems"]
        ]
        detail = "; ".join(grade.failures + problems) or "ok"
        print(f"  {case['id']}: {'✓' if grade.success else '✗'} {agent_run.status}  {detail}")
    await engine.dispose()
    print(f"{passed}/{len(cases)} cases satisfiable by the scripted planner")
    return 0 if passed == len(cases) else 1


# --- Reporting ---------------------------------------------------------------------------------


def print_summary(variant: str, rows: list[dict[str, Any]]) -> None:
    print(f"{variant}: {len(rows)} rows")
    for metric in METRICS:
        passes = sum(r["grade"][metric] for r in rows)
        lo, hi = wilson(passes, len(rows))
        rate = passes / max(len(rows), 1)
        print(f"  {metric:9} {passes}/{len(rows)} = {rate:.0%}  (95% CI {lo:.0%}-{hi:.0%})")
    if rows:
        print(f"  calls     mean {statistics.mean(r['model_calls'] for r in rows):.1f}")
        print(f"  latency   median {statistics.median(r['latency_s'] for r in rows):.1f}s")
        total = sum(r["cost_usd"] for r in rows)
        print(f"  cost      ${total:.4f} total, ${total / len(rows):.5f}/case")


def report() -> int:
    """One line per variant, to compare models (or prompts) side by side."""
    variants = sorted(p for p in FLOW_DIR.glob("*/results.jsonl"))
    if not variants:
        print(f"No results yet in {FLOW_DIR}")
        return 0
    header = f"{'variant':12} {'model':22} {'n':>3}  {'success':>14}  {'first try':>14}"
    print(header + f"  {'calls':>5}  {'$/case':>8}  {'latency':>7}")
    for path in variants:
        rows = read_rows(path)
        if not rows:
            continue
        n = len(rows)
        models = ",".join(sorted({r["model"] for r in rows}))

        def rate(metric: str, rows: list[dict[str, Any]] = rows, n: int = n) -> str:
            passes = sum(r["grade"][metric] for r in rows)
            lo, hi = wilson(passes, n)
            return f"{passes / n:4.0%} ({lo:.0%}-{hi:.0%})"

        print(
            f"{path.parent.name:12} {models:22} {n:3}  {rate('success'):>14}  "
            f"{rate('first_try'):>14}  {statistics.mean(r['model_calls'] for r in rows):5.1f}  "
            f"${sum(r['cost_usd'] for r in rows) / n:7.4f}  "
            f"{statistics.median(r['latency_s'] for r in rows):6.1f}s"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--variant", default="baseline", help="baseline, sonnet, v1, ...")
    parser.add_argument("--model", default=get_settings().agent_model)
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout-s", type=float, default=420)
    parser.add_argument("--attempts", type=int, default=2, help="tries per case on API errors")
    parser.add_argument(
        "--dry-run", action="store_true", help="scripted model: free, writes nothing"
    )
    parser.add_argument("--report", action="store_true", help="compare the stored variants")
    parser.add_argument(
        "--approve-harness",
        action="store_true",
        help="record the current runner/grader/cases/fixture as approved, then exit",
    )
    args = parser.parse_args()

    if args.report:
        return report()
    if args.approve_harness:
        print(HARNESS.approve())
        return 0
    if not args.dry_run and not HARNESS.approved():
        print(
            "The eval's runner, grader, cases or fixture changed since they were last approved.\n"
            "Review the changes, then run:\n"
            "  uv run python -m evals.trip_planner.run_eval --approve-harness",
            file=sys.stderr,
        )
        return 2
    if args.model not in PRICES:
        print(f"No prices for {args.model}; add it to app/services/pricing.py", file=sys.stderr)
        return 2

    asyncio.run(recreate_database())
    migrate()
    fixture = load_fixture_file()

    async def go() -> int:
        await load_fixture(fixture)
        return await run(args)

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
