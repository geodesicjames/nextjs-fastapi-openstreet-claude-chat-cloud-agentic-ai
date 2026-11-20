// GET /api/agent/runs/{id}/events?after=N -> the run's steps as server-sent
// events, passed through as they arrive. FastAPI ends the stream after ten
// minutes (the browser then reconnects with `after`), so allow a little more.
import { proxyToApi } from "@/lib/api";

export async function GET(
  request: Request,
  { params }: RouteContext<"/api/agent/runs/[id]/events">,
) {
  const { id } = await params;
  const after = new URL(request.url).searchParams.get("after") ?? "-1";
  const query = new URLSearchParams({ after });
  return proxyToApi(request, `/agent/runs/${encodeURIComponent(id)}/events?${query}`, {
    timeoutMs: 660_000,
  });
}
