# Places

A map-based "ask about places" web app. Browse an OpenStreetMap-based map, search
for places, and (with invited access) chat with Claude, which uses tools to search
places, compute routes and, eventually, plan multi-step trips as an agent.

> **Status:** Phase 1, project skeleton. The stack runs end-to-end and the home
> page reports live API, database and PostGIS status. Maps, auth and chat come next.

## Architecture

```
 Browser ──► Next.js (App Router, :3000)
                │  Server Components call the API server-side
                ▼
             FastAPI (:8000) ──► Postgres + PostGIS (:5432)
                │
                └──► (later) Anthropic API, Overpass, Nominatim
```

FastAPI owns all data, auth and LLM calls. Next.js never talks to Anthropic or
the database directly.

| Layer    | Tech                                                                 |
| -------- | -------------------------------------------------------------------- |
| Frontend | Next.js 16 (App Router, Cache Components), TypeScript, Tailwind v4   |
| Backend  | FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2.0 async, Alembic |
| Database | Postgres 17 + PostGIS 3.5                                            |
| Tooling  | uv, ruff, pyright, pytest · pnpm, ESLint, Prettier · pre-commit, gitleaks |
| CI       | GitHub Actions                                                       |

## Repo layout

```
backend/
  app/
    main.py        app factory, middleware, router registration
    config.py      typed settings from env vars (pydantic-settings)
    db.py          async engine + session factory
    deps.py        shared dependencies (DB session; later current user, chat access)
    models/        SQLAlchemy models
    schemas/       Pydantic request/response schemas
    routers/       API endpoints (APIRouter)
    services/      business logic
  alembic/         database migrations
  tests/
frontend/
  app/             routes (App Router)
  lib/api.ts       server-only API client
docker-compose.yml
```

## Local setup

### Prerequisites

- Docker (Docker Desktop on Windows/macOS; on WSL, enable WSL integration for your distro)
- For running tools outside Docker and for the git hooks:
  [uv](https://docs.astral.sh/uv/), Node 24 with pnpm (`corepack enable pnpm`), and
  [pre-commit](https://pre-commit.com/) (`uv tool install pre-commit`)

### Run everything with Docker

```bash
cp .env.example .env
docker compose up --build
```

- App: http://localhost:3000. The status card should show API OK, Database Connected, and the PostGIS version.
- API: http://localhost:8000/health, with interactive docs at http://localhost:8000/docs.

Migrations run automatically when the backend container starts.

### Git hooks (once per clone)

```bash
pre-commit install
```

Every commit is then checked for secrets (gitleaks) and linted and formatted (ruff, Prettier).

### Backend commands

Run from `backend/`. The tests need the database, so start it with `docker compose up -d db`.

```bash
uv sync                                       # install deps into .venv
uv run fastapi dev app/main.py                # run API on the host (auto-reload)
uv run pytest                                 # tests
uv run ruff check . && uv run ruff format .   # lint + format
uv run pyright                                # type check
uv run alembic upgrade head                   # apply migrations
uv run alembic revision --autogenerate -m "add places"   # new migration (review it!)
```

### Frontend commands

Run from `frontend/`:

```bash
pnpm install
pnpm dev            # http://localhost:3000 (uses API_URL, default http://localhost:8000)
pnpm lint
pnpm typecheck
pnpm format
pnpm build
```

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every PR and on pushes to `main`:

- **Backend:** ruff lint and format check, pyright, Alembic migrations, and pytest against a PostGIS service container
- **Frontend:** Prettier check, ESLint, TypeScript, and `next build`
- **Secrets:** gitleaks over the full git history

## Decisions

| Decision | Why |
| --- | --- |
| **FastAPI owns data, auth and LLM calls** | One trust boundary. The Anthropic key and DB credentials live only in the backend. |
| **Async SQLAlchemy 2.0 + asyncpg** | FastAPI is async end to end, and blocking DB calls inside `async def` would stall the event loop. |
| **Settings via pydantic-settings** | Typed, validated at startup, and configured with env vars only (12-factor). |
| **`/health` checks the DB and PostGIS** | One endpoint serves the status page, the container healthcheck and a smoke test. It returns 503 when the DB is down. |
| **Initial migration enables PostGIS** | The Docker image enables it already, but managed Postgres (e.g. RDS) doesn't. The migration keeps the schema reproducible. |
| **Tests use a real PostGIS database** | Spatial behaviour can't be meaningfully faked with SQLite or mocks. |
| **Server Components fetch via a `server-only` module** | `API_URL` and (later) auth cookies never reach browser JavaScript. |
| **pyright over mypy** | Faster, and the same engine as VS Code's Pylance, so editor and CI agree. |
| **uv and pnpm** | Fast, lockfile-first package managers with reproducible installs. |
