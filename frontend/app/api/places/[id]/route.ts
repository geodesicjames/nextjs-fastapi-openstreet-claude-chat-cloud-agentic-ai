// GET /api/places/{id} -> FastAPI GET /places/{id} (one place with its tags).
import { forwardToApi } from "@/lib/api";

export async function GET(request: Request, { params }: RouteContext<"/api/places/[id]">) {
  const { id } = await params;
  return forwardToApi(request, `/places/${encodeURIComponent(id)}`);
}
