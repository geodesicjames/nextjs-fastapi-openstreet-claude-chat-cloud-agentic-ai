// Runs in the browser before the app starts: report uncaught errors to
// Sentry (off unless built with NEXT_PUBLIC_SENTRY_DSN; see lib/sentry-options.ts).
import * as Sentry from "@sentry/nextjs";

import { sentryOptions } from "./lib/sentry-options";

Sentry.init(sentryOptions);
