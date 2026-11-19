# Frontend image, with a stage per use:
#   dev  - the source is bind-mounted by docker-compose.yml (next dev)
#   prod - production: a standalone Next.js server, non-root, no pnpm
# Build one with: docker build --target prod -t places-frontend .

FROM node:24-slim AS base
# Corepack ships with Node and installs the pnpm version pinned in
# package.json's "packageManager" field.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable pnpm
WORKDIR /app

FROM base AS deps
# Dependencies in their own layer, so code edits don't reinstall them.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile


# --- dev ------------------------------------------------------------------------
FROM deps AS dev
COPY . .
EXPOSE 3000
CMD ["pnpm", "dev", "--hostname", "0.0.0.0"]


# --- prod ---------------------------------------------------------------------------
FROM deps AS build
# Error reporting (Sentry). NEXT_PUBLIC_* values are written into the bundles
# at build time, so they're build arguments; empty means Sentry stays off.
ARG NEXT_PUBLIC_SENTRY_DSN=""
ARG NEXT_PUBLIC_SENTRY_RELEASE=""
ENV NEXT_PUBLIC_SENTRY_DSN=$NEXT_PUBLIC_SENTRY_DSN \
    NEXT_PUBLIC_SENTRY_RELEASE=$NEXT_PUBLIC_SENTRY_RELEASE
COPY . .
# Also copies MapLibre's worker into public/vendor (see package.json).
RUN pnpm build

FROM node:24-slim AS prod
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /app
# The standalone server plus the static assets and public files it serves.
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public
# The node image's built-in unprivileged user.
# (The health check sends the origin secret, which proxy.ts requires in production.)
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["node", "-e", "fetch('http://localhost:3000/', { headers: { 'x-origin-verify': process.env.ORIGIN_VERIFY_SECRET ?? '' } }).then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]
CMD ["node", "server.js"]
