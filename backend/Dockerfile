# Development image for the FastAPI backend.
# A slimmer multi-stage production image will come with the deploy step.
FROM python:3.13-slim

# uv binary from the official image (no pip install needed).
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Keep the venv outside /app so the source bind mount in docker-compose
    # does not hide it.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install dependencies first, in their own layer, so editing source code does
# not trigger a full reinstall on rebuild.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

EXPOSE 8000

# Apply migrations, then start the dev server with auto-reload.
CMD ["sh", "-c", "alembic upgrade head && fastapi dev app/main.py --host 0.0.0.0 --port 8000"]
