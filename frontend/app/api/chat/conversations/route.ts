// GET  /api/chat/conversations -> list the visitor's conversations.
// POST /api/chat/conversations -> start a new one.
import { proxyToApi } from "@/lib/api";

export async function GET(request: Request) {
  return proxyToApi(request, "/chat/conversations");
}

export async function POST(request: Request) {
  return proxyToApi(request, "/chat/conversations");
}
