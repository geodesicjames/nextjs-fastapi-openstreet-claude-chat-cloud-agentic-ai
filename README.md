# Places

A map-based "ask about places" web app. Browse an OpenStreetMap-based map, search
for places, and (with invited access) chat with Claude, which uses tools to search
places, compute routes and, eventually, plan multi-step trips as an agent.

> **Status:** Phase 1, foundation. The stack runs end-to-end, the home page reports
> live API, database and PostGIS status, and users can sign in with a magic link.
> Maps and chat come next.

## Architecture

```
 Browser ──► Next.js (App Router, :3000)      same origin: no CORS
                │  Server Components / Server Actions call the API
                │  server-side, forwarding the session cookie
                ▼
             FastAPI (:8000) ──► Postgres + PostGIS (:5432)
                │
                └──► (later) Anthropic API, Overpass, Nominatim
```

FastAPI owns all data, auth and LLM calls. Next.js never talks to Anthropic or
the database directly, and the browser never talks to FastAPI directly.

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
    deps.py        shared dependencies (DB session, current user, chat access)
    cli.py         management commands (`python -m app.cli make-admin ...`)
    security.py    random token generation and hashing
    models/        SQLAlchemy models
    schemas/       Pydantic request/response schemas
    routers/       API endpoints (APIRouter)
    services/      business logic
  alembic/         database migrations
  tests/
frontend/
  app/             routes (App Router)
  lib/api.ts       server-only API client (forwards the session cookie)
  lib/auth-actions.ts  Server Actions for sign-in and sign-out
  lib/access-actions.ts  Server Actions for requesting access and the admin page
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

### Signing in locally

Sign-in uses a magic link, with no passwords. In development, emails are not
sent. The link is printed to the backend log instead:

1. Go to http://localhost:3000/login and enter any email address.
2. Find the link: `docker compose logs backend | grep -A4 "sign-in link"`
3. Open it and press **Sign in**.

Links expire after 30 minutes and work once. Sessions last 30 days. Both
values are settings in `backend/app/config.py`, overridable by environment
variables (`LOGIN_LINK_LIFETIME_MINUTES`, `SESSION_LIFETIME_DAYS`).

### Chat access (invite-only)

Anyone can sign in, but chat needs a **chat grant**. To give yourself admin
rights and a grant (the equivalent of Django's `createsuperuser`):

```bash
docker compose exec backend python -m app.cli make-admin you@example.com
```

Each grant has a daily token budget (input + output tokens, resetting at
midnight UTC). A grant with no limit of its own uses `CHAT_DEFAULT_DAILY_TOKENS`
(50,000). Setting `CHAT_MODE=open` lets every signed-in user chat with the
default budget. `GET /chat/access` reports your status and today's usage.

How someone gets access:

1. They fill in the form at http://localhost:3000/request-access
   (`POST /access-requests`). Every admin gets an email.
2. An admin approves or denies it on http://localhost:3000/admin. Approving
   creates their account if needed, creates a grant with an optional daily
   limit, expiry and note, and emails them a sign-in link.
3. On the same page, admins can edit a grant's limit, expiry or note, or revoke
   it. Changes take effect on the user's next request.

The site header shows each signed-in user how many chat tokens they have left
today, or a "Request chat access" link if they have no grant. Admins also see
an **Admin** link. The underlying endpoints are documented at
http://localhost:8000/docs.

### Map data

`GET /places?bbox=west,south,east,north&category=cafe` returns places as
GeoJSON. They come from OpenStreetMap through the
[Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API) and are
cached in PostGIS:

- The world is divided into a fixed grid of cells about 2 km across. The first
  request for an area fetches its cells from Overpass in one query, for every
  category at once. Later requests within 30 days (`PLACE_CACHE_DAYS`) are
  answered from PostGIS alone.
- The public Overpass server asks apps that use it regularly to stay under
  about 100 queries and 10 MB a day, in total across all users. The backend
  counts its calls in `upstream_calls` and stops at `OVERPASS_DAILY_QUERY_BUDGET`
  (80) or `OVERPASS_DAILY_MB_BUDGET` (8). It runs one query at a time, and
  backs off exponentially when the server is busy (30 s, doubling to 10 min).
- When it can't fetch, it still returns what's cached, with
  `"complete": false` and an `incomplete_reason` (`budget`, `cooldown`,
  `upstream_error` or `busy`).
- Set `OSM_CONTACT` in `.env` so the User-Agent identifies you, as the
  usage policy asks. The main public instance is often at capacity. For
  development you can point `OVERPASS_URL` at another
  [public instance](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances).

### Git hooks (once per clone)

```bash
pre-commit install
```

Every commit is then checked for secrets (gitleaks) and linted and formatted (ruff, Prettier).

### Backend commands

Run from `backend/`. The tests need the database, so start it with `docker compose up -d db`.
Like Django, they never use the database in `DATABASE_URL` directly: they run against a
sibling `<name>_test` database (e.g. `app_test`), which pytest creates and migrates
automatically, so your dev data can't affect test results.

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

- **Backend:** ruff lint and format check, pyright, Alembic migrations, and pytest against a PostGIS service container (in its own `app_test` database)
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
| **Server Components fetch via a `server-only` module** | `API_URL` and the session cookie never reach browser JavaScript. |
| **Magic-link sign-in, no passwords** | Invited users (e.g. recruiters) never need to create a password, and there are no password hashes to protect. |
| **Server-side sessions, not JWTs** | The cookie holds a random token and the DB stores only its SHA-256 hash. Sign-out and revocation take effect immediately, as with Django's DB sessions. A JWT can't be revoked before it expires without a server-side denylist. |
| **Browser talks only to Next.js** | The session cookie belongs to the frontend's origin and Next.js forwards it to FastAPI server-side. No CORS, no cross-site cookies, and FastAPI can live on a private network in production. |
| **Sign-in link needs a button press** | Email security scanners open links in advance. If a GET request signed you in, a scanner could use up the single-use token first. |
| **Identical response for any email** | `POST /auth/login-link` always returns 202 with the same body, so it can't be used to find out who has an account. It is also rate-limited per address. |
| **Alembic connects with `search_path=public`** | The `postgis/postgis` image puts its `tiger` and `topology` schemas on the search path, and autogenerate would otherwise try to drop their tables. |
| **Tests roll back a transaction** | Each test runs in a transaction that is rolled back afterwards, like Django's `TestCase`, so tests are isolated and leave the DB clean. |
| **Chat gated by a single dependency** | Every chat and agent route depends on `require_chat_access`, which returns 401 (not signed in), 403 (no active grant) or 429 with `Retry-After` (daily budget used up). One place to audit, and opening chat to everyone is just `CHAT_MODE=open`. |
| **Token budget summed from a usage ledger** | Every Claude call writes a row to `llm_usage` (tokens and estimated cost). The daily budget is the sum of today's rows, and the same table will feed the cost dashboard. Budgets are checked before each call, so the most a call can go over is its `max_tokens`. |
| **`PATCH` sends only the fields that change** | The handler uses `model_dump(exclude_unset=True)`, so `{"daily_token_limit": null}` (go back to the default) differs from leaving the field out (keep it as it is). |
| **Approval email includes a `/login` fallback** | Sign-in links expire after 30 minutes, but an approval email may not be read for days. |
| **One active grant per user, enforced by Postgres** | A partial unique index (`WHERE revoked_at IS NULL`) rather than an app-level check, so two simultaneous approvals can't both succeed. Revoked grants are kept as history. |
| **Places cached by grid cell** | Panning around an area costs at most a few upstream queries, and each cell is fetched once for every category. Cells, rather than exact bboxes, make "have we fetched this?" a simple primary-key lookup. |
| **Overpass budget kept in the database** | The `upstream_calls` table is shared by every backend process and survives restarts. An in-memory counter would reset on each deploy and count per process. |
| **Incomplete results instead of errors** | When Overpass is busy or the budget is used up, the map still shows cached places and says why some are missing, rather than failing. |
| **Rate limits use the real client IP** | Requests reach FastAPI through Next.js, so the visitor's IP is in `X-Forwarded-For`. It's only trusted from `TRUSTED_PROXIES`, and read right to left, so clients can't dodge limits with a fake header. The counters are in memory for now; several backend instances would need Redis. |
| **pyright over mypy** | Faster, and the same engine as VS Code's Pylance, so editor and CI agree. |
| **uv and pnpm** | Fast, lockfile-first package managers with reproducible installs. |
