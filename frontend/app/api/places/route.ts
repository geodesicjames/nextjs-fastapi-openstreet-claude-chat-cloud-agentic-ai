// GET /api/places?bbox=...&category=... -> FastAPI GET /places (GeoJSON).
//
// A route handler is a plain HTTP endpoint inside Next.js (roughly a Django
// view that returns JSON). The map calls this from the browser on every pan.
import { forwardToApi } from "@/lib/api";

export async function GET(request: Request) {
  const { search } = new URL(request.url);
  return forwardToApi(request, `/places${search}`);
}
