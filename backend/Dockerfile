# Backend image, with a stage per use:
#   dev  - the source is bind-mounted by docker-compose.yml, with auto-reload
#   prod - production: dependencies only (no dev tools), non-root, no reload
# Build one with: docker build --target prod -t places-backend .

FROM python:3.13-slim AS base
# uv binary from the official image (no pip install needed).
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /usr/local/bin/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Keep the venv outside /app so the dev bind mount does not hide it.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app


# --- dev ------------------------------------------------------------------------
FROM base AS dev
# Dependencies in their own layer, so editing source doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen
COPY . .
EXPOSE 8000
# Apply migrations, then start the dev server with auto-reload.
CMD ["sh", "-c", "alembic upgrade head && fastapi dev app/main.py --host 0.0.0.0 --port 8000"]


# --- prod ---------------------------------------------------------------------------
FROM base AS build
COPY pyproject.toml uv.lock ./
# Runtime dependencies only: no pytest, ruff, pyright...
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim AS prod
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    ENVIRONMENT=production
# An unprivileged user: a compromised process can't modify the image's files.
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
# Only what runs in production (no tests, evals or dev config).
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
# Migrations are a separate step (`alembic upgrade head`, the compose
# `migrate` service), not part of starting the server.
CMD ["python", "-m", "app.serve"]
