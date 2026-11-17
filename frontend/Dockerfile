# Development image for the Next.js frontend.
# A standalone production build will come with the deploy step.
FROM node:24-slim

# Corepack ships with Node and installs the pnpm version pinned in
# package.json's "packageManager" field.
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable pnpm

WORKDIR /app

# Install dependencies in their own layer so code edits don't reinstall them.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

EXPOSE 3000

CMD ["pnpm", "dev", "--hostname", "0.0.0.0"]
