// Proxy (Next.js's name for what was called middleware): runs before every
// request is routed. Here it has one job: in production, refuse requests that
// didn't come through our CloudFront distribution.
//
// The server's firewall only admits CloudFront's IP ranges, but those ranges
// are shared by every CloudFront customer, so someone could point their own
// distribution at our server. Our distribution adds a secret header to every
// request it forwards; anything without it is refused.
//
// When ORIGIN_VERIFY_SECRET is unset (local development), every request is
// allowed. Proxy runs in the Node.js runtime, so process.env is read at
// request time and node:crypto is available.
import { timingSafeEqual } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";

const ORIGIN_VERIFY_HEADER = "x-origin-verify";

function matches(given: string, expected: string): boolean {
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  // Compare in constant time so the secret can't be guessed byte by byte.
  return a.length === b.length && timingSafeEqual(a, b);
}

export function proxy(request: NextRequest) {
  const expected = process.env.ORIGIN_VERIFY_SECRET;
  if (!expected) return NextResponse.next();

  const given = request.headers.get(ORIGIN_VERIFY_HEADER) ?? "";
  if (!matches(given, expected)) {
    return new NextResponse("Forbidden", { status: 403 });
  }
  return NextResponse.next();
}

// No matcher: run on every request, static files included.
