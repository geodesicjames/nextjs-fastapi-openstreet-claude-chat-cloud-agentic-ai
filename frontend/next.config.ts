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

export default nextConfig;
