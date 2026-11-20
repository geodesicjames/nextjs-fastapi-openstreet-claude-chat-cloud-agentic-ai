// Admin: everyone's recent agent runs, each linking to its full trace.
// A Server Component; FastAPI's require_admin decides who sees the data.
import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { RUN_ERRORS } from "@/lib/agent";
import { getAgentRuns } from "@/lib/api";
import { formatDateTime, formatUsd } from "@/lib/format";

export const metadata: Metadata = { title: "Agent runs · Admin · Places" };

export default function AgentRunsPage() {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-8 px-6 py-10">
      <div className="flex flex-col gap-1">
        <Link href="/admin" className="text-sm text-neutral-500 underline underline-offset-4">
          ← Admin
        </Link>
        <h1 className="text-2xl font-semibold">Agent runs</h1>
        <p className="text-sm text-neutral-500">
          The 50 most recent trip-planner runs. Open one to see every step. Times are UTC.
        </p>
      </div>
      <Suspense fallback={<p className="text-neutral-500">Loading…</p>}>
        <RunList />
      </Suspense>
    </main>
  );
}

async function RunList() {
  const runs = await getAgentRuns();
  if (runs === null) return <p>This page is for admins only.</p>;
  if (runs.length === 0) return <p className="text-neutral-500">No runs yet.</p>;

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-neutral-500">
          <tr className="border-b border-neutral-200 dark:border-neutral-800">
            <th className="py-2 pr-4 font-normal">Started</th>
            <th className="py-2 pr-4 font-normal">User</th>
            <th className="py-2 pr-4 font-normal">Goal</th>
            <th className="py-2 pr-4 font-normal">Status</th>
            <th className="py-2 pr-4 text-right font-normal">Calls</th>
            <th className="py-2 text-right font-normal">Cost</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className="border-b border-neutral-100 dark:border-neutral-900">
              <td className="py-2 pr-4 whitespace-nowrap">{formatDateTime(run.created_at)}</td>
              <td className="py-2 pr-4">{run.user_email}</td>
              <td className="max-w-xs py-2 pr-4">
                <Link
                  href={`/admin/agent/${run.id}`}
                  className="line-clamp-2 underline underline-offset-4"
                >
                  {run.goal}
                </Link>
              </td>
              <td className="py-2 pr-4" title={run.error ? RUN_ERRORS[run.error] : undefined}>
                {run.status.replace("_", " ")}
                {run.error && <span className="text-red-600"> ({run.error})</span>}
              </td>
              <td className="py-2 pr-4 text-right tabular-nums">{run.model_calls}</td>
              <td className="py-2 text-right tabular-nums">{formatUsd(run.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
