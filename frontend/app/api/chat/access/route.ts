// GET /api/chat/access -> FastAPI GET /chat/access (can this visitor chat?).
import { proxyToApi } from "@/lib/api";

export async function GET(request: Request) {
  return proxyToApi(request, "/chat/access");
}
