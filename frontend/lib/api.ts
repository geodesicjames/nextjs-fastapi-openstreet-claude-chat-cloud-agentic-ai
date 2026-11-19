// Server-side helpers for calling the FastAPI backend.
//
// `import "server-only"` makes the build fail if a Client Component ever
// imports this file, so API_URL and the session cookie can never end up in
// browser JavaScript. Only variables prefixed NEXT_PUBLIC_ are exposed to the
// browser; API_URL deliberately is not.
//
// The browser never calls FastAPI itself. It talks only to this Next.js app
// (same origin, so no CORS), and the code here calls FastAPI server-side,
// forwarding the browser's cookies so the backend knows who is signed in.
import "server-only";

import { cookies } from "next/headers";

// Mirrors HealthOut in backend/app/schemas/health.py.
export type Health = {
  status: "ok" | "degraded";
  database: {
    connected: boolean;
    postgis_version: string | null;
  };
};

// Mirrors UserOut in backend/app/schemas/auth.py.
export type User = {
  id: string;
  email: string;
  is_admin: boolean;
  created_at: string;
};

function apiUrl(path: string): string {
  // Inside Docker Compose this is http://backend:8000 (the service name);
  // when running `pnpm dev` on the host it is http://localhost:8000.
  const base = process.env.API_URL ?? "http://localhost:8000";
  return new URL(path, base).toString();
}

/**
 * Call the backend as the current visitor, forwarding their cookies.
 *
 * Reading cookies makes whatever calls this render per request, so it must
 * sit inside a <Suspense> boundary when used from a Server Component.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const cookieStore = await cookies();
  const headers = new Headers(init.headers);
  const cookieHeader = cookieStore.toString();
  if (cookieHeader) headers.set("cookie", cookieHeader);
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");

  return fetch(apiUrl(path), {
    ...init,
    headers,
    cache: "no-store",
    signal: init.signal ?? AbortSignal.timeout(5000),
  });
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

/** The signed-in user, or null if signed out (or the API is unreachable). */
export function getCurrentUser(): Promise<User | null> {
  return getJson<User>("/auth/me");
}

// --- Chat access ---------------------------------------------------------------

// Mirrors ChatAccessOut in backend/app/schemas/chat.py.
export type ChatAccess = {
  allowed: boolean;
  reason: "no_grant" | "budget_exhausted" | null;
  mode: "invite_only" | "open";
  // USD
  daily_budget_usd: number;
  spent_today_usd: number;
  remaining_usd: number;
  resets_at: string;
  grant_expires_at: string | null;
};

// Mirrors AccessRequestOut in backend/app/schemas/access.py.
export type AccessRequest = {
  id: string;
  email: string;
  message: string;
  status: "pending" | "approved" | "denied";
  created_at: string;
  decided_at: string | null;
};

// Mirrors GrantOut in backend/app/schemas/access.py.
export type Grant = {
  id: string;
  user_id: string;
  user_email: string;
  note: string;
  /** The grant's own daily budget (USD); null means it follows the default. */
  daily_budget_usd: number | null;
  effective_daily_budget_usd: number;
  spent_today_usd: number;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
};

/** GET a JSON endpoint as the current visitor; null on any error status. */
async function getJson<T>(path: string): Promise<T | null> {
  try {
    const res = await apiFetch(path);
    return res.ok ? ((await res.json()) as T) : null;
  } catch {
    return null;
  }
}

/** The visitor's chat access and today's usage, or null if signed out. */
export function getChatAccess(): Promise<ChatAccess | null> {
  return getJson<ChatAccess>("/chat/access");
}

/** Pending access requests (admins only; null otherwise). */
export function getPendingAccessRequests(): Promise<AccessRequest[] | null> {
  return getJson<AccessRequest[]>("/admin/access-requests");
}

/** Active grants with today's usage (admins only; null otherwise). */
export function getActiveGrants(): Promise<Grant[] | null> {
  return getJson<Grant[]>("/admin/grants");
}

// Mirrors UsageReportOut in backend/app/schemas/usage.py.
export type UsageTotals = {
  cost_usd: number;
  calls: number;
  /** Uncached input + cache writes + cache reads. */
  input_tokens: number;
  /** Includes thinking. */
  output_tokens: number;
};

export type UsageReport = {
  start: string;
  end: string;
  total: UsageTotals;
  /** Every UTC day in the period, oldest first, zeros included. */
  by_day: (UsageTotals & { day: string })[];
  by_user: (UsageTotals & { email: string })[];
  by_model: (UsageTotals & { model: string })[];
};

/** Claude spend over the last `days` UTC days (admins only; null otherwise). */
export function getUsageReport(days: number): Promise<UsageReport | null> {
  return getJson<UsageReport>(`/admin/usage?days=${days}`);
}

// --- Proxy for browser-side calls ----------------------------------------------------

/**
 * Forward a browser request to a public FastAPI endpoint and pass the answer
 * straight back (status, JSON body and Retry-After).
 *
 * Used by route handlers under app/api/ for data the browser loads itself,
 * like map places on every pan, so the browser still only talks to this
 * origin. X-Forwarded-For carries the visitor's IP so FastAPI's per-IP rate
 * limits apply to the visitor, not to this server. Next.js fills that header
 * from the socket if the browser didn't send one; in production the load
 * balancer in front appends the real address, which FastAPI reads first.
 */
export async function forwardToApi(request: Request, path: string): Promise<Response> {
  const headers = new Headers({ accept: "application/json" });
  const forwardedFor = request.headers.get("x-forwarded-for");
  if (forwardedFor) headers.set("x-forwarded-for", forwardedFor);

  let res: Response;
  try {
    res = await fetch(apiUrl(path), {
      headers,
      cache: "no-store",
      // Longer than FastAPI's own upstream timeouts, so its answer (possibly
      // "incomplete") arrives instead of us giving up first.
      signal: AbortSignal.timeout(20_000),
    });
  } catch (error) {
    console.error(`forwardToApi ${path}:`, error);
    return Response.json({ detail: "The map service is unavailable." }, { status: 502 });
  }

  const out = new Headers({ "content-type": "application/json" });
  const retryAfter = res.headers.get("retry-after");
  if (retryAfter) out.set("retry-after", retryAfter);
  return new Response(res.body, { status: res.status, headers: out });
}

/**
 * Forward a browser request to FastAPI *as the signed-in visitor* and stream
 * the answer back unchanged (status, body, and the headers that matter).
 *
 * Used by the chat and saved-places route handlers. Unlike forwardToApi it
 * passes the session cookie and the request body, and it streams the
 * response, so server-sent events from the chat endpoint reach the browser
 * as they are produced.
 *
 * For methods that change data it also checks the Origin header: browsers
 * always send it on cross-site requests, so a mismatch means another site is
 * trying to use the visitor's session (CSRF). The SameSite=Lax cookie already
 * blocks that; this is a second line of defence.
 */
export async function proxyToApi(
  request: Request,
  path: string,
  { timeoutMs = 20_000 }: { timeoutMs?: number } = {},
): Promise<Response> {
  const method = request.method.toUpperCase();
  if (!["GET", "HEAD"].includes(method)) {
    const origin = request.headers.get("origin");
    const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
    if (origin && new URL(origin).host !== host) {
      return Response.json({ detail: "Cross-site request refused." }, { status: 403 });
    }
  }

  const headers = new Headers({ accept: request.headers.get("accept") ?? "application/json" });
  const cookie = request.headers.get("cookie");
  if (cookie) headers.set("cookie", cookie);
  const forwardedFor = request.headers.get("x-forwarded-for");
  if (forwardedFor) headers.set("x-forwarded-for", forwardedFor);
  const body = ["GET", "HEAD"].includes(method) ? undefined : await request.text();
  if (body) headers.set("content-type", "application/json");

  let res: Response;
  try {
    res = await fetch(apiUrl(path), {
      method,
      headers,
      body,
      cache: "no-store",
      // Stop the upstream request if the browser goes away, or after the timeout.
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(timeoutMs)]),
    });
  } catch (error) {
    console.error(`proxyToApi ${method} ${path}:`, error);
    return Response.json({ detail: "The service is unavailable." }, { status: 502 });
  }

  const out = new Headers();
  for (const name of ["content-type", "retry-after", "cache-control"]) {
    const value = res.headers.get(name);
    if (value) out.set(name, value);
  }
  // Ask any proxy in front of Next.js not to buffer a streamed response.
  if (out.get("content-type")?.startsWith("text/event-stream")) out.set("x-accel-buffering", "no");
  return new Response(res.status === 204 ? null : res.body, { status: res.status, headers: out });
}
