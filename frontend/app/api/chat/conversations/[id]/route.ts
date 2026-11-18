// GET /api/chat/conversations/{id} -> one conversation with its messages.
import { proxyToApi } from "@/lib/api";

export async function GET(
  request: Request,
  { params }: RouteContext<"/api/chat/conversations/[id]">,
) {
  const { id } = await params;
  return proxyToApi(request, `/chat/conversations/${encodeURIComponent(id)}`);
}
