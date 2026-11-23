// GET    /api/chat/conversations/{id} -> one conversation with its messages.
// DELETE /api/chat/conversations/{id} -> delete the conversation.
import { proxyToApi } from "@/lib/api";

type Context = RouteContext<"/api/chat/conversations/[id]">;

export async function GET(request: Request, { params }: Context) {
  const { id } = await params;
  return proxyToApi(request, `/chat/conversations/${encodeURIComponent(id)}`);
}

export async function DELETE(request: Request, { params }: Context) {
  const { id } = await params;
  return proxyToApi(request, `/chat/conversations/${encodeURIComponent(id)}`);
}
