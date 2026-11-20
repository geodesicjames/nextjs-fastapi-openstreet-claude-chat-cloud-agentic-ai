// GET /api/agent/runs/{id} -> one run with all its steps.
import { proxyToApi } from "@/lib/api";

export async function GET(request: Request, { params }: RouteContext<"/api/agent/runs/[id]">) {
  const { id } = await params;
  return proxyToApi(request, `/agent/runs/${encodeURIComponent(id)}`);
}
