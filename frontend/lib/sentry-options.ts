// Sentry settings shared by the browser (instrumentation-client.ts) and the
// Next.js server (sentry.server.config.ts, sentry.edge.config.ts).
//
// Off unless NEXT_PUBLIC_SENTRY_DSN was set when the app was built: Next.js
// writes NEXT_PUBLIC_* values into the bundles at build time, so the deploy
// passes the DSN as a Docker build argument. A DSN only lets someone send
// errors to the project, not read them, and the browser has to have it anyway.
//
// Errors only, with no personal data: no user identity or IP, cookies,
// headers, request bodies, query strings or local variables. Query strings
// are also cut from URLs in events and breadcrumbs, since sign-in links carry
// their token there. No performance tracing (no tracesSampleRate) and no
// session replay.
import type { Breadcrumb, ErrorEvent } from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN || undefined;

/** "https://x/auth/verify?token=abc#y" -> "https://x/auth/verify" */
export function withoutQuery(url: string): string {
  return url.split(/[?#]/, 1)[0];
}

export function scrubEvent(event: ErrorEvent): ErrorEvent {
  if (event.request?.url) event.request.url = withoutQuery(event.request.url);
  if (event.request) delete event.request.query_string;
  return event;
}

export function scrubBreadcrumb(breadcrumb: Breadcrumb): Breadcrumb {
  // Navigation (from/to) and fetch/xhr (url) breadcrumbs hold URLs.
  const data = breadcrumb.data;
  if (data) {
    for (const key of ["from", "to", "url"]) {
      if (typeof data[key] === "string") data[key] = withoutQuery(data[key]);
    }
  }
  return breadcrumb;
}

export const sentryOptions = {
  dsn,
  enabled: dsn !== undefined,
  environment: process.env.NODE_ENV,
  // The commit being deployed, also a build argument.
  release: process.env.NEXT_PUBLIC_SENTRY_RELEASE || undefined,
  dataCollection: {
    userInfo: false,
    cookies: false,
    httpHeaders: false,
    httpBodies: [],
    urlQueryParams: false,
    genAI: { inputs: false, outputs: false },
    databaseQueryData: false,
    stackFrameVariables: false,
  },
  beforeSend: scrubEvent,
  beforeBreadcrumb: scrubBreadcrumb,
};
