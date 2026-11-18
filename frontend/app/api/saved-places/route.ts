// GET /api/saved-places -> the visitor's saved places.
import { proxyToApi } from "@/lib/api";

export async function GET(request: Request) {
  return proxyToApi(request, "/saved-places");
}
