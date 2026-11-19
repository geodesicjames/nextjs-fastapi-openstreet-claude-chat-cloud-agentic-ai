// Column chart of spend per UTC day: one series, so no legend (the section
// title names it). Plain HTML/CSS, no chart library:
// - columns at most 24px wide with a 2px gap, 4px rounded tops, growing from
//   one baseline;
// - a recessive top gridline labelled with the scale's maximum;
// - every column is focusable and shows its date, spend and calls in a
//   tooltip on hover or keyboard focus (CSS only), and has the same text as
//   its accessible name. The same numbers are in the table below the chart.
// Blue #2a78d6 (light) / #3987e5 (dark), checked for contrast against both
// page backgrounds.
import type { UsageTotals } from "@/lib/api";
import { formatDate, formatUsd } from "@/lib/format";

type Day = UsageTotals & { day: string };

/** Round up to 1, 2 or 5 x a power of ten, so the scale reads cleanly. */
function niceMax(value: number): number {
  if (value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 5, 10].find((m) => m * power >= value) ?? 10;
  return step * power;
}

export function DailySpendChart({ days }: { days: Day[] }) {
  const max = niceMax(Math.max(...days.map((d) => d.cost_usd)));
  const labelled = new Set([0, Math.floor((days.length - 1) / 2), days.length - 1]);

  return (
    <figure className="flex flex-col gap-1">
      <div className="flex items-end gap-2">
        <div className="relative h-40 flex-1">
          {/* Top gridline with the scale's maximum. */}
          <div className="absolute inset-x-0 top-0 border-t border-dashed border-neutral-200 dark:border-neutral-800" />
          <span className="absolute -top-2.5 right-0 bg-[var(--background)] pl-1 text-xs text-neutral-500">
            {formatUsd(max)}
          </span>
          <div className="absolute inset-0 flex items-end justify-between gap-0.5 border-b border-neutral-300 dark:border-neutral-700">
            {days.map((d, i) => {
              // Keep tooltips inside the chart: anchor them to the inside edge
              // near either end, centred elsewhere.
              const align =
                i < days.length / 4
                  ? "left-0"
                  : i >= (days.length * 3) / 4
                    ? "right-0"
                    : "left-1/2 -translate-x-1/2";
              const label = `${formatDate(d.day)}: ${formatUsd(d.cost_usd)}, ${d.calls} ${d.calls === 1 ? "call" : "calls"}`;
              return (
                <div
                  key={d.day}
                  tabIndex={0}
                  aria-label={label}
                  // The column's full height is the hover target, not just the bar.
                  className="group relative flex h-full max-w-6 flex-1 items-end justify-center rounded-sm outline-offset-2 focus-visible:outline-2 focus-visible:outline-neutral-500"
                >
                  {d.cost_usd > 0 && (
                    <div
                      className="w-full rounded-t-[4px] bg-[#2a78d6] group-hover:opacity-80 group-focus-visible:opacity-80 dark:bg-[#3987e5]"
                      style={{ height: `${Math.max((d.cost_usd / max) * 100, 1.5)}%` }}
                    />
                  )}
                  <div
                    // Same text as the column's aria-label; don't read it twice.
                    aria-hidden="true"
                    className={`pointer-events-none absolute bottom-full z-10 mb-1 hidden ${align} rounded-md border border-neutral-200 bg-[var(--background)] px-2 py-1 text-xs whitespace-nowrap shadow-sm group-hover:block group-focus-visible:block dark:border-neutral-800`}
                  >
                    <strong className="font-semibold tabular-nums">{formatUsd(d.cost_usd)}</strong>{" "}
                    <span className="text-neutral-500">
                      {formatDate(d.day)} · {d.calls} {d.calls === 1 ? "call" : "calls"}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
      <div className="flex justify-between text-xs text-neutral-500">
        {days.map((d, i) =>
          labelled.has(i) ? <span key={d.day}>{formatDate(d.day)}</span> : null,
        )}
      </div>
      <figcaption className="sr-only">
        Spend per day from {formatDate(days[0].day)} to {formatDate(days[days.length - 1].day)}
      </figcaption>
    </figure>
  );
}
