// GET  /api/agent/runs -> the visitor's recent trip-planning runs.
// POST /api/agent/runs -> start one ({goal}); it runs in the background.
import { proxyToApi } from "@/lib/api";

export async function GET(request: Request) {
  return proxyToApi(request, "/agent/runs");
}

export async function POST(request: Request) {
  return proxyToApi(request, "/agent/runs");
}
