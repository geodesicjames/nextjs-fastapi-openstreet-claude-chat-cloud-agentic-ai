// POST /api/agent/runs/{id}/cancel -> stop the run.
import { proxyToApi } from "@/lib/api";

export async function POST(
  request: Request,
  { params }: RouteContext<"/api/agent/runs/[id]/cancel">,
) {
  const { id } = await params;
  return proxyToApi(request, `/agent/runs/${encodeURIComponent(id)}/cancel`);
}
