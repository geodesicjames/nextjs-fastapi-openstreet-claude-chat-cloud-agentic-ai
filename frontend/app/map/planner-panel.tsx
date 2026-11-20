// The "Plan" tab: describe an outing and the trip-planner agent searches for
// places, proposes an itinerary, checks it in code, and waits for your
// approval. You can approve it, send it back with changes, or cancel.
//
// The run lives on the server (a background worker drives it), so this
// component only starts it and watches. Progress arrives as server-sent
// events of saved steps; if the stream drops, it reconnects asking only for
// steps after the last one it has. Reloading the page reopens the latest
// unfinished run where it is.
"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  describeStep,
  formatDistance,
  formatMinutes,
  isActive,
  type Run,
  type RunDetail,
  RUN_ERRORS,
  type Step,
  type StepLine,
  type TripPlan,
} from "@/lib/agent";
import { type Access, errorText, fetchAccess, readSse } from "@/lib/chat";
import { formatUsd } from "@/lib/format";
import { categorySingular } from "@/lib/map";

import { AccessNotice, canUseAssistant } from "./access-notice";

const MAX_CHARS = 2000; // mirrors CHAT_MAX_MESSAGE_CHARS
// Wait this long before reopening a stream that ended while the run was
// still going (the server closes streams after ten minutes; networks drop).
const RECONNECT_MS = 2000;

/** Somewhere in the plan to show on the map; the start has no place_id. */
export type PlanStopTarget = { place_id: string | null; lat: number; lon: number };

export function PlannerPanel({
  onPlan,
  onShow,
}: {
  /** The itinerary to draw on the map, or null to clear it. */
  onPlan: (plan: TripPlan | null) => void;
  onShow: (stop: PlanStopTarget) => void;
}) {
  const router = useRouter();
  const [access, setAccess] = useState<Access>({ kind: "loading" });
  const [recent, setRecent] = useState<Run[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The last step seen, for reconnecting. A ref, not state: the stream
  // reads it when it reconnects, and changing it shouldn't re-render.
  const lastSeq = useRef(-1);

  const refreshRecent = useCallback(async (): Promise<Run[]> => {
    const res = await fetch("/api/agent/runs");
    const list = res.ok ? ((await res.json()) as Run[]) : [];
    setRecent(list);
    return list;
  }, []);

  const show = useCallback((detail: RunDetail) => {
    const { steps: loaded, ...rest } = detail;
    setRun(rest);
    setSteps(loaded);
    lastSeq.current = loaded.at(-1)?.seq ?? -1;
  }, []);

  const open = useCallback(
    async (id: string) => {
      setProblem(null);
      const res = await fetch(`/api/agent/runs/${encodeURIComponent(id)}`);
      if (!res.ok) return setProblem("Couldn't open that plan.");
      show((await res.json()) as RunDetail);
    },
    [show],
  );

  // On load: who is this, and is a plan already under way?
  useEffect(() => {
    void fetchAccess().then(async (result) => {
      setAccess(result);
      if (!canUseAssistant(result)) return;
      const latest = (await refreshRecent())[0];
      if (latest && (isActive(latest.status) || latest.status === "awaiting_approval")) {
        await open(latest.id);
      }
    });
  }, [open, refreshRecent]);

  // While the run is moving, stream its new steps.
  const runId = run?.id;
  const moving = run ? isActive(run.status) : false;
  useEffect(() => {
    if (!runId || !moving) return;
    const controller = new AbortController();
    void watchRun(runId, () => lastSeq.current, controller.signal, {
      onStep(step) {
        if (step.seq <= lastSeq.current) return; // already have it
        lastSeq.current = step.seq;
        setSteps((current) => [...current, step]);
      },
      onRun(stopped) {
        setRun(stopped);
        void refreshRecent();
        router.refresh(); // update the header's budget badge
      },
      onProblem: setProblem,
    });
    return () => controller.abort();
  }, [runId, moving, refreshRecent, router]);

  // Draw the itinerary once there is a checked one to show.
  const plan =
    run?.plan && (run.status === "awaiting_approval" || run.status === "done") ? run.plan : null;
  useEffect(() => {
    onPlan(plan);
  }, [plan, onPlan]);

  /** POST to the API; on success, show the run it returns. */
  async function act(path: string, body?: object): Promise<boolean> {
    setBusy(true);
    setProblem(null);
    try {
      const res = await fetch(path, {
        method: "POST",
        headers: body ? { "content-type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!res.ok) {
        setProblem(await errorText(res));
        // The run may have moved on (e.g. finished while you were reading).
        if (res.status === 409 && run) await open(run.id);
        return false;
      }
      setRun((await res.json()) as Run);
      return true;
    } catch (error) {
      console.error(error);
      setProblem("The connection was interrupted. Please try again.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const goal = String(new FormData(form).get("goal") ?? "").trim();
    if (goal.length < 3 || busy) return;
    setSteps([]);
    lastSeq.current = -1;
    if (await act("/api/agent/runs", { goal })) {
      form.reset();
      void refreshRecent();
    }
  }

  async function requestChanges(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!run) return;
    const form = event.currentTarget;
    const text = String(new FormData(form).get("text") ?? "").trim();
    if (!text || busy) return;
    if (await act(`/api/agent/runs/${encodeURIComponent(run.id)}/changes`, { text })) form.reset();
  }

  function startOver() {
    setRun(null);
    setSteps([]);
    setProblem(null);
    lastSeq.current = -1;
  }

  if (!canUseAssistant(access)) {
    return <AccessNotice access={access} purpose="plan a walk with the assistant" />;
  }
  const outOfBudget = access.info.reason === "budget_exhausted";
  const lines = steps.flatMap(describeStep);

  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="flex items-center gap-2">
        <label htmlFor="plan-run" className="sr-only">
          Plan
        </label>
        <select
          id="plan-run"
          value={run?.id ?? ""}
          onChange={(e) => (e.target.value ? void open(e.target.value) : startOver())}
          className="min-w-0 flex-1 truncate rounded-md border border-neutral-300 bg-transparent px-2 py-1 dark:border-neutral-700"
        >
          <option value="">New plan</option>
          {recent.map((r) => (
            <option key={r.id} value={r.id}>
              {r.goal}
            </option>
          ))}
        </select>
        <span className="shrink-0 text-xs text-neutral-500" title="Left of today's chat budget">
          {formatUsd(access.info.remaining_usd)} left
        </span>
      </div>

      {!run ? (
        <form onSubmit={start} className="flex flex-col gap-2">
          <p className="text-neutral-500">
            Describe an outing, e.g. “Coffee, then a park, near Leith Walk, back by 4pm” or “Two
            hours of viewpoints from the Royal Mile”. The planner searches for places, proposes an
            itinerary and checks it (every stop real, walks short, times adding up) before asking
            you to approve it.
          </p>
          <label htmlFor="plan-goal" className="sr-only">
            What would you like to do?
          </label>
          <textarea
            id="plan-goal"
            name="goal"
            rows={3}
            minLength={3}
            maxLength={MAX_CHARS}
            required
            disabled={busy || outOfBudget}
            placeholder="What would you like to do?"
            className="rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700"
          />
          <button
            type="submit"
            disabled={busy || outOfBudget}
            className="self-end rounded-md bg-neutral-900 px-3 py-1.5 text-white disabled:opacity-60 dark:bg-white dark:text-neutral-900"
          >
            {busy ? "Starting…" : "Plan it"}
          </button>
        </form>
      ) : (
        <>
          <p className="self-end rounded-lg bg-neutral-100 px-3 py-2 whitespace-pre-wrap dark:bg-neutral-800">
            {run.goal}
          </p>
          <ol className="flex flex-col gap-1 text-xs text-neutral-500" aria-live="polite">
            {lines.map((line) => (
              <ProgressLine key={line.key} line={line} />
            ))}
            {isActive(run.status) && <li>⋯ {lines.length ? "Working…" : "Starting…"}</li>}
          </ol>

          {plan && (
            <PlanCard
              plan={plan}
              onShow={onShow}
              onShowStart={() =>
                onShow({ place_id: null, lat: plan.start.lat, lon: plan.start.lon })
              }
            />
          )}

          <RunStatusNote run={run} />

          {run.status === "awaiting_approval" && (
            <div className="flex flex-col gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => void act(`/api/agent/runs/${encodeURIComponent(run.id)}/approve`)}
                className="self-start rounded-md bg-neutral-900 px-3 py-1.5 text-white disabled:opacity-60 dark:bg-white dark:text-neutral-900"
              >
                Approve plan
              </button>
              <form onSubmit={requestChanges} className="flex flex-col gap-2">
                <label htmlFor="plan-changes" className="text-neutral-600 dark:text-neutral-400">
                  Or ask for changes
                </label>
                <textarea
                  id="plan-changes"
                  name="text"
                  rows={2}
                  maxLength={MAX_CHARS}
                  required
                  disabled={busy || outOfBudget}
                  placeholder="e.g. Swap the café for one with outdoor seating"
                  className="rounded-md border border-neutral-300 bg-transparent px-3 py-2 dark:border-neutral-700"
                />
                <button
                  type="submit"
                  disabled={busy || outOfBudget}
                  className="self-end rounded-md border border-neutral-300 px-3 py-1.5 disabled:opacity-60 dark:border-neutral-700"
                >
                  Send back
                </button>
              </form>
            </div>
          )}

          <div className="flex items-center justify-between gap-2 text-xs text-neutral-500">
            <span>
              {run.model_calls} model call{run.model_calls === 1 ? "" : "s"} ·{" "}
              {formatUsd(run.cost_usd)}
            </span>
            {isActive(run.status) || run.status === "awaiting_approval" ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => void act(`/api/agent/runs/${encodeURIComponent(run.id)}/cancel`)}
                className="underline underline-offset-4 disabled:opacity-60"
              >
                Cancel plan
              </button>
            ) : (
              <button type="button" onClick={startOver} className="underline underline-offset-4">
                Plan something else
              </button>
            )}
          </div>
        </>
      )}

      {problem && (
        <p role="alert" className="text-red-600">
          {problem}
        </p>
      )}
      {outOfBudget && (
        <p className="text-amber-700 dark:text-amber-400">
          You&apos;ve used today&apos;s chat budget. It resets at midnight UTC.
        </p>
      )}
    </div>
  );
}

/**
 * Stream a run's steps after `after()` until the run stops moving.
 *
 * Reconnects when a stream ends early (the server's ten-minute limit, a
 * dropped connection, a deploy), each time asking only for newer steps.
 */
async function watchRun(
  id: string,
  after: () => number,
  signal: AbortSignal,
  handlers: {
    onStep: (step: Step) => void;
    onRun: (run: Run) => void;
    onProblem: (message: string) => void;
  },
): Promise<void> {
  while (!signal.aborted) {
    try {
      const query = new URLSearchParams({ after: String(after()) });
      const res = await fetch(`/api/agent/runs/${encodeURIComponent(id)}/events?${query}`, {
        signal,
      });
      if (res.status < 500 && (!res.ok || !res.body)) {
        handlers.onProblem(await errorText(res));
        return; // signed out, or the run is gone: retrying won't help
      }
      if (res.ok && res.body) {
        for await (const { event, data } of readSse(res.body)) {
          if (event === "step") handlers.onStep(data as Step);
          else if (event === "run") return handlers.onRun(data as Run);
        }
      }
    } catch (error) {
      if (signal.aborted) return;
      console.error(error);
    }
    await new Promise((resolve) => setTimeout(resolve, RECONNECT_MS));
  }
}

const ICONS: Record<StepLine["icon"], string> = { ok: "✓", error: "✕", info: "·", you: "↩" };

function ProgressLine({ line }: { line: StepLine }) {
  return (
    <li className={line.icon === "error" ? "text-amber-700 dark:text-amber-400" : undefined}>
      <span aria-hidden="true">{ICONS[line.icon]} </span>
      <span className={line.icon === "info" ? "whitespace-pre-wrap" : undefined}>{line.text}</span>
      {line.more && line.more.length > 0 && (
        <ul className="ml-4 list-disc">
          {line.more.map((m, i) => (
            <li key={i}>{m}</li>
          ))}
        </ul>
      )}
    </li>
  );
}

function RunStatusNote({ run }: { run: Run }) {
  switch (run.status) {
    case "awaiting_approval":
      return <p className="font-medium">Does this plan work for you?</p>;
    case "done":
      return <p className="font-medium text-green-700 dark:text-green-400">✓ Plan approved.</p>;
    case "cancelled":
      return <p className="text-neutral-500">Plan cancelled.</p>;
    case "failed":
      return (
        <p role="alert" className="text-red-600">
          {RUN_ERRORS[run.error ?? ""] ?? RUN_ERRORS.internal}
        </p>
      );
    default:
      return null;
  }
}

/** A numbered marker, matching the ones drawn on the map. */
function Badge({ label }: { label: string }) {
  return (
    <span className="inline-flex size-5 shrink-0 items-center justify-center rounded-full bg-neutral-900 text-[11px] font-bold text-white dark:bg-white dark:text-neutral-900">
      {label}
    </span>
  );
}

function PlanCard({
  plan,
  onShow,
  onShowStart,
}: {
  plan: TripPlan;
  onShow: (stop: PlanStopTarget) => void;
  onShowStart: () => void;
}) {
  const walk = (minutes: number, metres: number) =>
    `${formatMinutes(minutes)} walk (${formatDistance(metres)})`;
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-neutral-200 p-3 dark:border-neutral-800">
      <div>
        <h2 className="font-medium">{plan.title}</h2>
        <p className="text-neutral-600 dark:text-neutral-400">{plan.summary}</p>
      </div>
      <ol className="flex flex-col gap-2.5">
        <li className="flex gap-2">
          <Badge label="S" />
          <div>
            <button type="button" onClick={onShowStart} className="text-left hover:underline">
              {plan.start.name}
            </button>
            {plan.start_time && <span className="text-neutral-500"> · {plan.start_time}</span>}
          </div>
        </li>
        {plan.stops.map((stop, i) => (
          <li key={stop.place_id} className="flex gap-2">
            <Badge label={String(i + 1)} />
            <div className="flex min-w-0 flex-col gap-0.5">
              <p className="text-xs text-neutral-500">{walk(stop.walk_minutes, stop.walk_m)}</p>
              <button
                type="button"
                onClick={() => onShow(stop)}
                className="text-left font-medium hover:underline"
              >
                {stop.name ?? `Unnamed ${categorySingular(stop.category)}`}
              </button>
              <p className="text-xs text-neutral-500">
                {capitalize(categorySingular(stop.category))} ·{" "}
                {stop.arrive ? `${stop.arrive}–${stop.leave}` : `${formatMinutes(stop.minutes)}`}
              </p>
              <p className="text-neutral-600 dark:text-neutral-400">{stop.why}</p>
            </div>
          </li>
        ))}
        {plan.return && (
          <li className="flex gap-2">
            <Badge label="S" />
            <div>
              <p className="text-xs text-neutral-500">
                {walk(plan.return.walk_minutes, plan.return.walk_m)}
              </p>
              <p>
                Back to {plan.start.name}
                {plan.return.arrive && (
                  <span className="text-neutral-500"> · {plan.return.arrive}</span>
                )}
              </p>
            </div>
          </li>
        )}
      </ol>
      <p>
        About {formatMinutes(plan.total_minutes)} in all, {formatDistance(plan.total_walk_m)} on
        foot.
      </p>
      <p className="text-xs text-neutral-500">{plan.note}</p>
    </section>
  );
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
