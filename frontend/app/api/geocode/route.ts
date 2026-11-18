// GET /api/geocode?q=... -> FastAPI GET /geocode. Called on search submit only:
// Nominatim's usage policy forbids search-as-you-type.
import { forwardToApi } from "@/lib/api";

export async function GET(request: Request) {
  const q = new URL(request.url).searchParams.get("q") ?? "";
  return forwardToApi(request, `/geocode?${new URLSearchParams({ q })}`);
}
