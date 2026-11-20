// Trip-planner types, mirroring backend/app/schemas/agent.py and the plan
// built by check_plan in backend/app/services/trip_planner.py, plus how each
// step is described in the panel.

import { describeTool } from "@/lib/chat";

export type RunStatus =
  "queued" | "running" | "awaiting_approval" | "done" | "failed" | "cancelled";

/** Statuses in which the run is still moving (the worker has it, or will). */
export const ACTIVE: readonly RunStatus[] = ["queued", "running"];

export function isActive(status: RunStatus): boolean {
  return ACTIVE.includes(status);
}

export type TripStop = {
  place_id: string;
  /** From the database, never from the model; null for unnamed places. */
  name: string | null;
  category: string;
  lat: number;
  lon: number;
  /** Time spent at the stop. */
  minutes: number;
  why: string;
  /** The (estimated) walk here from the previous stop or the start. */
  walk_m: number;
  walk_minutes: number;
  /** "HH:MM", when the plan has a start time. */
  arrive?: string;
  leave?: string;
};

export type TripPlan = {
  title: string;
  summary: string;
  categories: string[];
  start: { name: string; lat: number; lon: number };
  start_time: string | null;
  end_by: string | null;
  max_minutes: number | null;
  stops: TripStop[];
  /** The walk back to the start, if the plan returns there. */
  return: { walk_m: number; walk_minutes: number; arrive?: string } | null;
  total_walk_m: number;
  total_minutes: number;
  note: string;
};

export type Run = {
  id: string;
  task: string;
  goal: string;
  status: RunStatus;
  error: string | null;
  /** The checked itinerary, once there is one. */
  plan: TripPlan | null;
  model_calls: number;
  cost_usd: number;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
};

export type StepKind = "goal" | "model" | "tool" | "check" | "feedback" | "nudge";

export type Step = {
  seq: number;
  kind: StepKind;
  created_at: string;
  duration_ms: number | null;
  cost_usd: number | null;
  // Depends on kind; see StepOut in backend/app/schemas/agent.py.
  detail: Record<string, unknown>;
};

export type RunDetail = Run & { steps: Step[] };

/** Why a run failed, in words, by its `error` code. */
export const RUN_ERRORS: Record<string, string> = {
  step_limit: "The planner ran out of steps before finishing.",
  check_failed: "The planner couldn't produce a plan that passed the checks.",
  cost_limit: "The planner hit its spending limit for one plan.",
  time_limit: "The planner took too long.",
  budget: "You've used today's chat budget. It resets at midnight UTC.",
  no_access: "Your access to the assistant has ended.",
  unavailable: "The assistant isn't available right now. Please try again later.",
  refused: "The assistant declined this request.",
  truncated: "The assistant's reply was cut off.",
  internal: "Something went wrong on our side.",
};

export type StepLine = {
  key: string;
  icon: "ok" | "error" | "info" | "you";
  text: string;
  /** Further lines, e.g. the problems a check found. */
  more?: string[];
};

/**
 * One step as lines for the progress list, or none for steps people don't
 * need to see (the goal, shown above the list, and the loop's own nudges).
 */
export function describeStep(step: Step): StepLine[] {
  const d = step.detail;
  const key = String(step.seq);
  switch (step.kind) {
    case "model": {
      const text = typeof d.text === "string" ? d.text.trim() : "";
      return text ? [{ key, icon: "info", text }] : [];
    }
    case "tool": {
      const name = String(d.name ?? "");
      const input = (d.input ?? {}) as Record<string, unknown>;
      const summary = typeof d.summary === "string" && d.summary ? ` · ${d.summary}` : "";
      return [
        { key, icon: d.is_error ? "error" : "ok", text: describeTool(name, input) + summary },
      ];
    }
    case "check": {
      if (d.ok) return [{ key, icon: "ok", text: "Proposed a plan; it passed the checks" }];
      const problems = Array.isArray(d.problems) ? d.problems.map(String) : [];
      return [
        { key, icon: "error", text: "Proposed a plan; the checks found problems", more: problems },
      ];
    }
    case "feedback":
      return [{ key, icon: "you", text: `You asked for changes: “${String(d.text ?? "")}”` }];
    default:
      return [];
  }
}

/** 1250 -> "1.3 km", 400 -> "400 m". */
export function formatDistance(metres: number): string {
  return metres >= 1000 ? `${(metres / 1000).toFixed(1)} km` : `${Math.round(metres)} m`;
}

/** 95 -> "1 h 35 min", 40 -> "40 min". */
export function formatMinutes(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h} h${m ? ` ${m} min` : ""}` : `${m} min`;
}
