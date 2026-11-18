// DELETE /api/saved-places/{id} -> remove a saved place.
import { proxyToApi } from "@/lib/api";

export async function DELETE(request: Request, { params }: RouteContext<"/api/saved-places/[id]">) {
  const { id } = await params;
  return proxyToApi(request, `/saved-places/${encodeURIComponent(id)}`);
}
