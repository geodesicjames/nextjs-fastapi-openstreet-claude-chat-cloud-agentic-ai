# Project brief

## What this is
A portfolio project: a map-based "ask about places" web app. Users browse an
OpenStreetMap-based map, and (with invited access) chat with Claude, which uses
tools to search places, compute routes, and eventually plan multi-step trips
as an agent.

Goal: demonstrate production-minded skills hiring managers look for —
Next.js + FastAPI, LLM integration (streaming, tool use, evals), agentic AI,
cloud deployment, geospatial work. Code quality, tests, CI, and clear
documentation matter as much as features. The developer has a strong Django
background and is building FastAPI/Next.js experience; prefer idiomatic,
well-explained patterns over clever ones.

Theme: TBD (e.g. dog-friendly hikes, bike routes) — keep naming generic until decided.

## Stack
- **Frontend:** Next.js (App Router), TypeScript, Tailwind. Server Components by
  default; `"use client"` only where interactivity is needed.
- **Backend:** FastAPI (async), Pydantic v2, pydantic-settings, SQLAlchemy 2.0
  (async, `asyncpg`), Alembic migrations.
- **Database:** Postgres + PostGIS (pgvector later for RAG).
- **Local dev:** Docker Compose runs everything with one command.
- **Tooling:** uv for Python deps; ruff (lint + format); pyright or mypy;
  pytest + httpx AsyncClient. pnpm, ESLint, Prettier for the frontend;
  Playwright for e2e later.
- **CI:** GitHub Actions — lint, type check, tests on every PR.
- **Cloud (later):** one cloud, likely AWS (ECS Fargate, RDS), Terraform, GitHub OIDC deploys.

## Repo layout
Monorepo:
```
/frontend   Next.js app
/backend    FastAPI app
  app/
    main.py
    config.py     pydantic-settings
    db.py         engine/session
    deps.py       shared dependencies (db session, current user, chat access)
    models/       SQLAlchemy models
    schemas/      Pydantic schemas (separate Create/Update/Out)
    routers/      APIRouters
    services/     business logic
  alembic/
  tests/
/infra      Terraform (later)
docker-compose.yml
```

## Architecture decisions
- FastAPI is the source of truth for data, auth, and all LLM calls. Next.js
  calls FastAPI (from Server Components or the browser) and never talks to
  Anthropic directly.
- Auth is owned by FastAPI (JWT in an httpOnly cookie). Magic-link login is
  planned so invited recruiters never need a password.
- Use `async def` only with async libraries; never call blocking I/O inside
  `async def` endpoints.
- Separate input/output schemas; always set `response_model` / return types so
  internal fields (password hashes, etc.) are never exposed.

## Security rules (non-negotiable)
- The Anthropic API key lives only in backend env/secrets. Never in frontend
  code and never in a `NEXT_PUBLIC_*` variable.
- `.env` files are gitignored from the first commit; provide `.env.example`.
  Add secret scanning (e.g. gitleaks pre-commit hook).
- The public map and place search are open. Light rate limiting is applied, especially on
  endpoints that proxy Overpass/Nominatim (respect their usage policies; cache
  results in PostGIS).
- Chat and agent routes are **invite-only**:
  - `ChatGrant` table (user, granted_by, note, daily_token_limit, expires_at, revoked_at).
  - A single `require_chat_access` dependency guards every chat/agent route and
    enforces the per-grant daily token budget.
  - Request-access form → notify owner → owner approves → magic link emailed.
  - `chat_mode` setting (`invite_only` | `open`) so opening up later is a config change.
  - Log tokens + estimated cost per call; `max_tokens` on every call; cap
    message length and agent steps.
  - Dedicated Anthropic API key for this project with a Console spend limit.

## Roadmap
1. **Foundation:** monorepo, Docker Compose (Postgres+PostGIS), FastAPI
   skeleton with health check + DB, Next.js skeleton calling the API, auth,
   CI. Deploy something early.
2. **Maps:** MapLibre/Leaflet frontend; Overpass + Nominatim via backend;
   PostGIS caching and spatial queries; optional OSRM/Valhalla routing.
3. **Claude chat:** SSE streaming, tool use (search_places, get_route,
   save_place), conversation history, invite-only gating, tool-call evals.
4. **Cloud & ops:** Terraform, managed Postgres, background jobs, Sentry,
   OpenTelemetry, structured logs, cost dashboard.
5. **Agentic trip planner:** hand-written agent loop first (plan → act →
   check → revise), guardrails, human-in-the-loop approval, persisted and
   resumable runs, step tracing, goal-based evals. Optionally compare with a
   framework.
6. **Stretch:** RAG with pgvector, planner/critic multi-agent, scheduled agent,
   MCP server exposing place/route tools.

## Working style
- Work in small, reviewable steps. Propose a plan before large changes.
- One GitHub issue → branch → PR per unit of work; clear commit messages.
- Every feature ships with tests; keep CI green.
- Keep the README current: pitch, demo GIF/link, architecture diagram, local
  setup, and a "Decisions" section recording why choices were made.
- Explain FastAPI/Next.js patterns briefly when introducing them (the developer
  is coming from Django).
