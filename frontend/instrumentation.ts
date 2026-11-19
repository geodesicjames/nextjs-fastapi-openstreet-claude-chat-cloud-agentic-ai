// Next.js calls register() once when the server starts (the "instrumentation"
// hook), so this is where server-side error reporting is set up.
import * as Sentry from "@sentry/nextjs";

export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") await import("./sentry.server.config");
  if (process.env.NEXT_RUNTIME === "edge") await import("./sentry.edge.config");
}

// Errors thrown while rendering Server Components, in route handlers, Server
// Actions and proxy.ts.
export const onRequestError = Sentry.captureRequestError;
