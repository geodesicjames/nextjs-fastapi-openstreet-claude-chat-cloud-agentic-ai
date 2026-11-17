// Home page: a Server Component (the default in the App Router).
//
// It runs on the server, can be `async`, and can await data directly, much
// like a Django view that renders a template. No `useEffect`, no client-side
// fetch, and no JavaScript is shipped to the browser for this component.
//
// With Cache Components (enabled in next.config.ts) data is fetched fresh on
// every request unless you opt in with "use cache". The static parts of the
// page are prerendered at build time, and the live status streams in at
// request time inside the <Suspense> boundary, which shows the fallback until
// the backend has answered.
import { Suspense } from "react";

import { getHealth } from "@/lib/api";

export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-xl flex-1 flex-col gap-6 px-6 py-16">
      <header>
        <h1 className="text-3xl font-semibold">Places</h1>
        <p className="mt-2 text-neutral-600 dark:text-neutral-400">
          Map-based place search with an AI assistant. Coming soon.
        </p>
      </header>

      <section className="rounded-lg border border-neutral-200 p-5 dark:border-neutral-800">
        <h2 className="mb-3 text-sm font-medium tracking-wide text-neutral-500 uppercase">
          System status
        </h2>
        <Suspense fallback={<p className="text-neutral-500">Checking…</p>}>
          <HealthStatus />
        </Suspense>
      </section>
    </main>
  );
}

async function HealthStatus() {
  const health = await getHealth();

  const rows: [label: string, value: string, ok: boolean][] = health
    ? [
        ["API", health.status === "ok" ? "OK" : "Degraded", health.status === "ok"],
        [
          "Database",
          health.database.connected ? "Connected" : "Unreachable",
          health.database.connected,
        ],
        [
          "PostGIS",
          health.database.postgis_version ?? "Unknown",
          health.database.postgis_version !== null,
        ],
      ]
    : [["API", "Unreachable", false]];

  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2">
      {rows.map(([label, value, ok]) => (
        <div key={label} className="contents">
          <dt className="text-neutral-600 dark:text-neutral-400">{label}</dt>
          <dd className="flex items-center gap-2 font-mono">
            <span
              aria-hidden
              className={`size-2 rounded-full ${ok ? "bg-green-500" : "bg-red-500"}`}
            />
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
