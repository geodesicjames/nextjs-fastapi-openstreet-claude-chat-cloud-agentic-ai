import { withSentryConfig } from "@sentry/nextjs/config";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Production builds include a self-contained server (.next/standalone)
  // with only the dependencies it uses, so the production image doesn't
  // need node_modules or pnpm.
  output: "standalone",
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

// Sentry's build step wires its SDK into the server and browser bundles.
// Source maps aren't uploaded (that would need a Sentry auth token in CI), so
// browser stack traces show the minified code; server traces are readable.
export default withSentryConfig(nextConfig, {
  silent: true,
  telemetry: false, // don't send Sentry's build plugin usage data
  sourcemaps: { disable: true },
  release: { create: false },
});
