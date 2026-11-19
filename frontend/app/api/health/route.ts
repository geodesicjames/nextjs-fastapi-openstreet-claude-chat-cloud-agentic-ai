// GET /api/health: is the whole stack up (Next.js, FastAPI, the database)?
// The deploy workflow calls it through CloudFront after every deploy. Only
// the overall status is returned; versions and details stay private.
import { connection } from "next/server";

import { getHealth } from "@/lib/api";

export async function GET() {
  // Check at request time, never at build time.
  await connection();
  const health = await getHealth();
  const status = health?.status ?? "unavailable";
  return Response.json(
    { status },
    { status: status === "ok" ? 200 : 503, headers: { "cache-control": "no-store" } },
  );
}
