// Sentry for the Next.js server (Node.js runtime), loaded by instrumentation.ts.
import * as Sentry from "@sentry/nextjs";

import { sentryOptions } from "./lib/sentry-options";

Sentry.init(sentryOptions);
