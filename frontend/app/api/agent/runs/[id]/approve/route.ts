// POST /api/agent/runs/{id}/approve -> accept the checked plan.
import { proxyToApi } from "@/lib/api";

export async function POST(
  request: Request,
  { params }: RouteContext<"/api/agent/runs/[id]/approve">,
) {
  const { id } = await params;
  return proxyToApi(request, `/agent/runs/${encodeURIComponent(id)}/approve`);
}
