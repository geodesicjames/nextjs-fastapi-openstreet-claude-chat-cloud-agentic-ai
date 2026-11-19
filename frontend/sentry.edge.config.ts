// Sentry for any code on Next.js's edge runtime, loaded by instrumentation.ts.
import * as Sentry from "@sentry/nextjs";

import { sentryOptions } from "./lib/sentry-options";

Sentry.init(sentryOptions);
