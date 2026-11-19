// Admin cost page: Claude spend by day, user and model, from FastAPI's
// llm_usage ledger (GET /admin/usage). Days are UTC, like the budgets.
//
// A Server Component throughout: the chart is plain HTML and CSS, its hover
// tooltips are CSS too, so the page ships no JavaScript of its own.
import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { getUsageReport, type UsageReport, type UsageTotals } from "@/lib/api";
import { formatDate, formatUsd } from "@/lib/format";

import { DailySpendChart } from "./daily-spend-chart";

export const metadata: Metadata = { title: "Claude spend · Admin · Places" };

const PERIODS = [7, 30, 90] as const;
const tokens = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export default function UsagePage({ searchParams }: PageProps<"/admin/usage">) {
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-8 px-6 py-10">
      <div className="flex flex-col gap-1">
        <Link href="/admin" className="text-sm text-neutral-500 underline underline-offset-4">
          ← Admin
        </Link>
        <h1 className="text-2xl font-semibold">Claude spend</h1>
        <p className="text-sm text-neutral-500">
          Estimated from token usage at list prices. Days are UTC.
        </p>
      </div>
      {/* searchParams and the cookie are only known per request. */}
      <Suspense fallback={<p className="text-neutral-500">Loading…</p>}>
        <UsageContent searchParams={searchParams} />
      </Suspense>
    </main>
  );
}

async function UsageContent({ searchParams }: Pick<PageProps<"/admin/usage">, "searchParams">) {
  const { days: daysParam } = await searchParams;
  const days = PERIODS.find((p) => String(p) === daysParam) ?? 30;
  const report = await getUsageReport(days);

  if (report === null) {
    return <p>This page is for admins only.</p>;
  }

  return (
    <>
      <nav aria-label="Period" className="flex gap-2 text-sm">
        {PERIODS.map((p) => (
          <Link
            key={p}
            href={`/admin/usage?days=${p}`}
            aria-current={p === days ? "page" : undefined}
            className={
              p === days
                ? "rounded-md bg-neutral-900 px-3 py-1 text-white dark:bg-neutral-100 dark:text-neutral-900"
                : "rounded-md border border-neutral-300 px-3 py-1 dark:border-neutral-700"
            }
          >
            {p} days
          </Link>
        ))}
      </nav>

      <Summary report={report} />

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">Spend per day</h2>
        <DailySpendChart days={report.by_day} />
        <details className="text-sm">
          <summary className="cursor-pointer text-neutral-500">Show as a table</summary>
          <BreakdownTable
            label="Day"
            rows={report.by_day
              .filter((d) => d.calls > 0)
              .reverse()
              .map((d) => ({ key: d.day, name: formatDate(d.day), ...d }))}
            empty="Nothing spent in this period."
          />
        </details>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">By user</h2>
        <BreakdownTable
          label="User"
          rows={report.by_user.map((u) => ({ key: u.email, name: u.email, ...u }))}
          empty="No one has used chat in this period."
        />
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold">By model</h2>
        <BreakdownTable
          label="Model"
          rows={report.by_model.map((m) => ({ key: m.model, name: m.model, ...m }))}
          empty="No model calls in this period."
        />
      </section>
    </>
  );
}

function Summary({ report }: { report: UsageReport }) {
  const { total } = report;
  const tiles = [
    { label: "Spend", value: formatUsd(total.cost_usd) },
    { label: "Model calls", value: total.calls.toLocaleString("en-US") },
    { label: "Input tokens", value: tokens.format(total.input_tokens) },
    { label: "Output tokens", value: tokens.format(total.output_tokens) },
  ];
  return (
    <section aria-label="Totals" className="flex flex-col gap-2">
      <p className="text-sm text-neutral-500">
        {formatDate(report.start)} – {formatDate(report.end)}
      </p>
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {tiles.map((t) => (
          <div
            key={t.label}
            className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800"
          >
            <dt className="text-sm text-neutral-500">{t.label}</dt>
            <dd className="text-2xl font-semibold tabular-nums">{t.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

type Row = UsageTotals & { key: string; name: string };

function BreakdownTable({ label, rows, empty }: { label: string; rows: Row[]; empty: string }) {
  if (rows.length === 0) return <p className="text-sm text-neutral-500">{empty}</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-neutral-500">
          <tr className="border-b border-neutral-200 dark:border-neutral-800">
            <th className="py-2 pr-4 font-normal">{label}</th>
            <th className="py-2 pr-4 text-right font-normal">Spend</th>
            <th className="py-2 pr-4 text-right font-normal">Calls</th>
            <th className="py-2 pr-4 text-right font-normal">Input tokens</th>
            <th className="py-2 text-right font-normal">Output tokens</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((r) => (
            <tr key={r.key} className="border-b border-neutral-100 dark:border-neutral-900">
              <td className="py-2 pr-4">{r.name}</td>
              <td className="py-2 pr-4 text-right">{formatUsd(r.cost_usd)}</td>
              <td className="py-2 pr-4 text-right">{r.calls.toLocaleString("en-US")}</td>
              <td className="py-2 pr-4 text-right">{tokens.format(r.input_tokens)}</td>
              <td className="py-2 text-right">{tokens.format(r.output_tokens)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
