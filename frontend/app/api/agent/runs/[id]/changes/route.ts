// POST /api/agent/runs/{id}/changes -> send it back with feedback ({text}); it runs again.
import { proxyToApi } from "@/lib/api";

export async function POST(
  request: Request,
  { params }: RouteContext<"/api/agent/runs/[id]/changes">,
) {
  const { id } = await params;
  return proxyToApi(request, `/agent/runs/${encodeURIComponent(id)}/changes`);
}
