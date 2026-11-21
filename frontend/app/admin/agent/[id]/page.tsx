// Admin: one agent run, step by step, as the model saw it: every model call
// (text, tool calls, tokens, cost, time), every tool call with the exact
// output sent back, every check and its problems, and any feedback.
//
// A Server Component with no JavaScript of its own: long outputs fold away
// in <details> elements.
import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { RUN_ERRORS } from "@/lib/agent";
import { getAgentRunTrace, type RunTrace, type TraceStep } from "@/lib/api";
import { formatDateTime, formatUsd } from "@/lib/format";

export const metadata: Metadata = { title: "Agent run · Admin · Places" };

const tokens = new Intl.NumberFormat("en-US");

export default function AgentRunPage({ params }: PageProps<"/admin/agent/[id]">) {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-8 px-6 py-10">
      <Link href="/admin/agent" className="text-sm text-neutral-500 underline underline-offset-4">
        ← Agent runs
      </Link>
      <Suspense fallback={<p className="text-neutral-500">Loading…</p>}>
        <Trace params={params} />
      </Suspense>
    </main>
  );
}

async function Trace({ params }: Pick<PageProps<"/admin/agent/[id]">, "params">) {
  const { id } = await params;
  const trace = await getAgentRunTrace(id);
  if (trace === null) return <p>Run not found, or this page is for admins only.</p>;

  return (
    <>
      <Header trace={trace} />
      <ol className="flex flex-col gap-3">
        {trace.steps.map((step) => (
          <li
            key={step.seq}
            className="flex flex-col gap-2 rounded-lg border border-neutral-200 p-3 text-sm dark:border-neutral-800"
          >
            <StepHeading step={step} />
            <StepBody step={step} />
          </li>
        ))}
      </ol>
      {trace.plan && (
        <details className="text-sm">
          <summary className="cursor-pointer text-neutral-500">The stored plan (JSON)</summary>
          <Json value={trace.plan} />
        </details>
      )}
    </>
  );
}

function Header({ trace }: { trace: RunTrace }) {
  // Time spent working (model calls and tools), not waiting for the user.
  const seconds = trace.steps.reduce((sum, step) => sum + (step.duration_ms ?? 0), 0) / 1000;
  const t = trace.tokens;
  const facts: [string, string][] = [
    ["User", trace.user_email],
    ["Status", trace.status.replace("_", " ") + (trace.error ? ` (${trace.error})` : "")],
    ["Model", trace.model],
    ["Started", `${formatDateTime(trace.created_at)} UTC`],
    ["Working time", `${seconds.toFixed(0)} s in model calls and tools`],
    ["Model calls", String(trace.model_calls)],
    ["Cost", formatUsd(trace.cost_usd)],
    [
      "Tokens",
      `${tokens.format(t.input_tokens)} in (+${tokens.format(t.cache_read_input_tokens)} cache read, ` +
        `${tokens.format(t.cache_creation_input_tokens)} cache write), ${tokens.format(t.output_tokens)} out`,
    ],
  ];
  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-2xl font-semibold">{trace.goal}</h1>
      {trace.error && <p className="text-red-600">{RUN_ERRORS[trace.error] ?? trace.error}</p>}
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        {facts.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-neutral-500">{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

const KIND_LABELS: Record<TraceStep["kind"], string> = {
  goal: "Goal",
  model: "Model call",
  tool: "Tool",
  check: "Check",
  feedback: "Feedback from the user",
  nudge: "Nudge from the loop",
};

function StepHeading({ step }: { step: TraceStep }) {
  const extras = [
    step.duration_ms !== null ? `${(step.duration_ms / 1000).toFixed(1)} s` : null,
    step.cost_usd !== null ? formatUsd(step.cost_usd) : null,
  ].filter(Boolean);
  return (
    <div className="flex items-baseline justify-between gap-2">
      <h2 className="font-medium">
        <span className="text-neutral-500 tabular-nums">#{step.seq}</span> {KIND_LABELS[step.kind]}
        {step.kind === "tool" && `: ${String(step.data.name)}`}
        {step.kind === "check" && (step.data.ok ? " ✓ passed" : " ✕ failed")}
      </h2>
      <span className="text-xs text-neutral-500 tabular-nums">{extras.join(" · ")}</span>
    </div>
  );
}

type Block = { type: string; text?: string; thinking?: string; name?: string; input?: unknown };

function StepBody({ step }: { step: TraceStep }) {
  const d = step.data;
  switch (step.kind) {
    case "model": {
      const blocks = (d.content ?? []) as Block[];
      const usage = [
        ["in", d.input_tokens],
        ["cache read", d.cache_read_input_tokens],
        ["cache write", d.cache_creation_input_tokens],
        ["out", d.output_tokens],
      ]
        .filter(([, n]) => typeof n === "number")
        .map(([label, n]) => `${tokens.format(n as number)} ${label}`);
      return (
        <>
          {blocks.map((b, i) => {
            if (b.type === "thinking") {
              return b.thinking ? (
                <details key={i}>
                  <summary className="cursor-pointer text-neutral-500">Thinking</summary>
                  <p className="whitespace-pre-wrap text-neutral-600 dark:text-neutral-400">
                    {b.thinking}
                  </p>
                </details>
              ) : null;
            }
            if (b.type === "redacted_thinking") {
              return (
                <p key={i} className="text-neutral-500">
                  (redacted thinking)
                </p>
              );
            }
            if (b.type === "text") {
              return (
                <p key={i} className="whitespace-pre-wrap">
                  {b.text}
                </p>
              );
            }
            if (b.type === "tool_use") {
              return (
                <div key={i}>
                  <p className="text-neutral-500">Calls {b.name}:</p>
                  <Json value={b.input} />
                </div>
              );
            }
            return <Json key={i} value={b} />;
          })}
          <p className="text-xs text-neutral-500">
            Stopped: {String(d.stop_reason)} · {usage.join(", ")}
          </p>
        </>
      );
    }
    case "tool":
      return (
        <>
          <Json value={d.input} />
          <p className={d.is_error ? "text-red-600" : "text-neutral-600 dark:text-neutral-400"}>
            {d.is_error ? "Error: " : ""}
            {String(d.summary ?? "")}
          </p>
          <details>
            <summary className="cursor-pointer text-neutral-500">Output sent to the model</summary>
            <Json value={parseJson(d.output)} />
          </details>
        </>
      );
    case "check": {
      const problems = Array.isArray(d.problems) ? d.problems.map(String) : [];
      return (
        <>
          {problems.length > 0 && (
            <ul className="ml-4 list-disc text-amber-700 dark:text-amber-400">
              {problems.map((p, i) => (
                <li key={i}>{p}</li>
              ))}
            </ul>
          )}
          {d.result ? (
            <details>
              <summary className="cursor-pointer text-neutral-500">Sent to the model</summary>
              <Json value={parseJson(d.result)} />
            </details>
          ) : (
            <p className="text-neutral-500">Paused for the user&apos;s approval.</p>
          )}
        </>
      );
    }
    case "nudge":
      return (
        <p className="whitespace-pre-wrap">
          <span className="text-neutral-500">({String(d.reason)}) </span>
          {String(d.text ?? "")}
        </p>
      );
    default: // goal, feedback
      return <p className="whitespace-pre-wrap">{String(d.text ?? "")}</p>;
  }
}

/** Tool outputs are stored as the JSON text the model received; show them parsed. */
function parseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function Json({ value }: { value: unknown }) {
  return (
    <pre className="max-h-96 overflow-auto rounded-md bg-neutral-50 p-2 text-xs dark:bg-neutral-900">
      {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
    </pre>
  );
}
