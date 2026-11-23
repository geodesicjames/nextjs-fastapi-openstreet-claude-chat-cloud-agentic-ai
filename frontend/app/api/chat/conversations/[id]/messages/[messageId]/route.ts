// DELETE /api/chat/conversations/{id}/messages/{messageId} -> delete a question
// and its answer, so the assistant forgets them.
import { proxyToApi } from "@/lib/api";

export async function DELETE(
  request: Request,
  { params }: RouteContext<"/api/chat/conversations/[id]/messages/[messageId]">,
) {
  const { id, messageId } = await params;
  return proxyToApi(
    request,
    `/chat/conversations/${encodeURIComponent(id)}/messages/${encodeURIComponent(messageId)}`,
  );
}
