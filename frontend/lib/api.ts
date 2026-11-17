// Server-side helpers for calling the FastAPI backend.
//
// `import "server-only"` makes the build fail if a Client Component ever
// imports this file, so API_URL (and later, forwarded auth cookies) can never
// end up in browser JavaScript. Only variables prefixed NEXT_PUBLIC_ are
// exposed to the browser; API_URL deliberately is not.
import "server-only";

// Mirrors HealthOut in backend/app/schemas/health.py.
export type Health = {
  status: "ok" | "degraded";
  database: {
    connected: boolean;
    postgis_version: string | null;
  };
};

function apiUrl(path: string): string {
  // Inside Docker Compose this is http://backend:8000 (the service name);
  // when running `pnpm dev` on the host it is http://localhost:8000.
  const base = process.env.API_URL ?? "http://localhost:8000";
  return new URL(path, base).toString();
}

/** Fetch backend health. Returns null if the API is unreachable. */
export async function getHealth(): Promise<Health | null> {
  try {
    // The backend answers 503 with a "degraded" body when the database is
    // down, so read the JSON whatever the status code.
    const res = await fetch(apiUrl("/health"), {
      signal: AbortSignal.timeout(3000),
    });
    return (await res.json()) as Health;
  } catch {
    return null;
  }
}
