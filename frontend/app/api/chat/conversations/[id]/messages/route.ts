// POST /api/chat/conversations/{id}/messages -> send a message; the reply
// streams back as server-sent events, passed through as they arrive.
import { proxyToApi } from "@/lib/api";

export async function POST(
  request: Request,
  { params }: RouteContext<"/api/chat/conversations/[id]/messages">,
) {
  const { id } = await params;
  // A tool-using reply can take a while; allow up to two minutes.
  return proxyToApi(request, `/chat/conversations/${encodeURIComponent(id)}/messages`, {
    timeoutMs: 120_000,
  });
}
