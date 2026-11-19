# Places

A map-based "ask about places" web app. Browse an OpenStreetMap-based map, search
for places, and (with invited access) chat with Claude, which uses tools to search
places, compute routes and, eventually, plan multi-step trips as an agent.

> **Status:** Phase 2, maps. `/map` shows OpenStreetMap places, cached in
> PostGIS, with place search. Magic-link sign-in and invite-only chat access are
> in place. Claude chat comes next.

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
  app/map/         the map page (MapLibre GL, a Client Component)
  app/api/         route handlers the map and chat call from the browser (proxy to FastAPI)
  lib/chat.ts      chat types and the streamed-reply (SSE) reader
  lib/markdown.tsx tiny Markdown renderer for chat replies (React elements only)
  scripts/         copy-maplibre-worker.mjs (see Decisions)
docker-compose.yml       development stack (source mounted, auto-reload)
docker-compose.prod.yml  production stack (see "Production stack")
scripts/backup-db.sh     database backup (pg_dump, optional upload to S3)
infra/                   Terraform for AWS (see infra/README.md)
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

After pulling changes that add dependencies, rebuild the affected image. For
the frontend, also renew its `node_modules` volume. Running `pnpm install`
inside the running container can leave its packages half-linked.

```bash
docker compose up -d --build backend
docker compose up -d --build --renew-anon-volumes frontend
```

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

Each grant has a **daily budget in US dollars**, estimated from the tokens
each Claude call uses and resetting at midnight UTC. A grant with no budget of
its own uses `CHAT_DAILY_BUDGET_USD` ($1.00). Setting `CHAT_MODE=open` lets
every signed-in user chat with the default budget. `GET /chat/access` reports
your status and today's spend.

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
  usage policies ask.
- The default instance is the community-run `ovp.dresel.systems`, because the
  main `overpass-api.de` was often at capacity during development. Point
  `OVERPASS_URL` at any other
  [public instance](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances)
  to change it. The budgets follow the main instance's stricter policy
  whichever you use.

### Claude chat

Chat needs an Anthropic API key in `.env`. Use a key dedicated to this
project, created in a workspace with a spend limit in the
[Claude Console](https://platform.claude.com). Without a key the app runs
normally and the chat endpoints answer 503.

| Setting | Default | What it does |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | (empty) | Backend only. Never in a `NEXT_PUBLIC_*` variable. |
| `CHAT_MODEL` | `claude-haiku-5-5` | The model the app's chat calls. It must have prices in `backend/app/services/pricing.py`, or the backend won't start. |
| `CHAT_EFFORT` | `low` | How much the model reasons: `low` to `max`. Higher costs more. |
| `CHAT_DAILY_BUDGET_USD` | `1.00` | Default daily spend per user. Per-user amounts are set on `/admin`. |
| `CHAT_MAX_TOKENS` | `16000` | Output cap for every call (thinking counts towards it). |
| `CHAT_MAX_MESSAGE_CHARS` | `2000` | Longest message a user can send. |
| `CHAT_MAX_TOOL_STEPS` | `6` | Most tool steps per message. After that, one last call with tools disabled makes the model answer with what it has. |

Compose passes the whole `.env` to the backend, so changing one of these
means editing `.env` and running `docker compose up -d backend`.

`POST /chat/conversations/{id}/messages` streams the reply as server-sent
events: `text`, `tool_start` / `tool_result` (what it's looking up, with
places to show on the map), `error`, and always a final `done`.

**Tools.** Claude answers from the app's own data with four tools, backed by
the same services as the map. That means the Overpass and Nominatim budgets
and caches apply to chat too:

| Tool | What it does |
| --- | --- |
| `find_location` | Geocodes a place name (Nominatim, cached) |
| `search_places` | Places of given categories within a radius, nearest first (PostGIS cache, Overpass on a miss) |
| `get_place_details` | Opening hours, address, website and other OSM tags for one place |
| `save_place` | Saves a place to the user's list (`GET` / `DELETE /saved-places`) | Tests use a scripted fake model. One
opt-in test calls the real API for a fraction of a cent:
`RUN_LIVE_CLAUDE=1 uv run pytest tests/test_chat_live.py -s`.

### The map

http://localhost:3000/map shows a vector map (MapLibre GL with
[OpenFreeMap](https://openfreemap.org) tiles; no API key). Zoom in to level 13
or closer and places load for the visible area, refreshing as you pan. You can
filter by category, search for a place (on submit), and click a place for its
opening hours, website and a link to OpenStreetMap.

The side panel has three tabs:
- **Explore:** the search, filters and list above.
- **Ask:** chat with the assistant, for invited users. Replies stream in, each
  tool step shows as a status line ("Searching cafés within 500 m · Found 27
  places"), and the places it finds appear on the map as larger, outlined
  markers.
- **Saved:** places the assistant saved for you. To use another tile
provider, set `NEXT_PUBLIC_MAP_STYLE_URL` to its style URL. Tile keys are
public by design.

### Place search (geocoding)

`GET /geocode?q=Leith Walk` finds towns, streets and addresses with
[Nominatim](https://nominatim.openstreetmap.org), following its
[usage policy](https://operations.osmfoundation.org/policies/nominatim/):

- **At most one request per second** across the whole backend. Searches queue
  for their turn, and give up as "busy" after 5 seconds.
- **Every answer is cached for 30 days, including "no results"**. The policy
  warns that clients repeating the same query may be blocked.
- **Search on submit, never on each keystroke.** Autocomplete is forbidden.
  Each IP may make 20 searches a minute.
- The same exponential backoff as Overpass, a daily safety budget
  (`NOMINATIM_DAILY_QUERY_BUDGET`, 1000), and older cached results
  (`"stale": true`) if Nominatim can't be reached.

### Evals

`backend/evals/chat_tools/` is an eval of the chat's tool use. It runs 26 questions
through the real chat code with the real model, against frozen Edinburgh map data.
It grades whether the right tools were called with sensible arguments (`tool_use`, checked
in code) and whether the answer only states what the tools returned (`grounded`, checked
by a Sonnet 5.5 judge). The baseline on Haiku 5.5 is **98% tool_use and 81% grounded**. See
`.claude/hillclimb/chat-tools/metrics.md` for the metrics, the limitations and the findings.

```bash
cd backend
uv run python -m evals.chat_tools.run_eval --approve-harness  # after reviewing any eval change
uv run python -m evals.chat_tools.run_eval --reps 2 --variant v1  # a new variant (~$0.55, mostly the judge)
uv run python -m evals.chat_tools.run_eval --regrade           # re-judge stored answers only
uv run python -m evals.chat_tools.check_judge                  # judge sanity checks (~$0.08)
```

Runs use real API calls, so they're manual, never in CI. The runner refuses to run if
its runner, grader, cases or fixture changed since you last approved them. The grader's
code checks have offline tests (`tests/test_eval_grader.py`) that run in CI. Results go
to `.claude/hillclimb/chat-tools/<variant>/`; the report is built with the Claude API
skill's `build-report-lite.mjs`.

### Production stack

`docker-compose.prod.yml` is what runs on the server, and you can run it locally too:

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm migrate   # apply migrations
FRONTEND_PORT=3100 docker compose -f docker-compose.prod.yml up -d
scripts/backup-db.sh                                         # pg_dump; restore steps inside
```

It uses the `prod` stages of the Dockerfiles. The backend has no dev dependencies, runs as a
non-root user with a single uvicorn worker and no reload, and writes JSON logs. The frontend is a
Next.js `standalone` server running as the `node` user. Only the frontend publishes a port.
Migrations are an explicit step. Settings come from `ENV_FILE` (`.env` by default). CI builds both
production images on every PR.

Deploying to AWS is set up in `infra/` (Terraform). `infra/README.md` walks through account
security, SSO sign-in for the CLI, the one-time bootstrap, and the GitHub `production`
environment.

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
| **Budgets in dollars, summed from a usage ledger** | Every Claude call writes a row to `llm_usage` with its tokens, including cache reads and writes, and an estimated cost from a per-model price table. Dollars rather than tokens, because cached, uncached and output tokens differ in price by up to 50x, so a token count says little about what a user actually costs. The daily budget is the sum of today's `cost_usd`, checked before each call, so the most a call can go over is its `max_tokens`. |
| **Claude behind a small `ChatModel` interface** | The chat service never imports the SDK. `AnthropicChatModel` streams through `AsyncAnthropic`, and tests use a scripted fake, so the whole flow is tested without network calls or API cost. |
| **Chat history stored exactly as sent** | Each message's API content blocks are stored as JSONB and replayed unchanged, including thinking blocks, whose text is empty but whose signature must be kept. History is append-only: editing earlier turns would invalidate replayed thinking and the prompt cache. A failed call saves nothing, so history never has a question without its answer. |
| **A hand-written tool loop** | Calling the model, running the tools it asks for, and calling it again is a short loop in `app/services/chat.py`, not the SDK's tool runner. Owning it is what allows a budget check before every call, a cap on tool steps, SSE events for each tool, and saving the turn only when it can be replayed. |
| **Strict tool schemas instead of eager input streaming** | Tool inputs here are tiny (coordinates, a place name), so streaming them early gains nothing. `strict: true` makes the API guarantee inputs match the schema. Bounds that strict schemas can't express (radius 50–2000 m, note length) are enforced by Pydantic, and an out-of-range value goes back to Claude as an error it can correct. |
| **Only replayable turns are saved** | A turn that ends on an unanswered or cut-off tool call (refusal, `max_tokens`, budget running out, API failure) would be rejected when replayed, so its messages are dropped. Every call's cost is still logged. |
| **One reply at a time per conversation** | Sending locks the conversation row with `FOR UPDATE NOWAIT`. A double-submitted message gets "busy" rather than a second, interleaved reply. |
| **`PATCH` sends only the fields that change** | The handler uses `model_dump(exclude_unset=True)`, so `{"daily_token_limit": null}` (go back to the default) differs from leaving the field out (keep it as it is). |
| **Approval email includes a `/login` fallback** | Sign-in links expire after 30 minutes, but an approval email may not be read for days. |
| **One active grant per user, enforced by Postgres** | A partial unique index (`WHERE revoked_at IS NULL`) rather than an app-level check, so two simultaneous approvals can't both succeed. Revoked grants are kept as history. |
| **Places cached by grid cell** | Panning around an area costs at most a few upstream queries, and each cell is fetched once for every category. Cells, rather than exact bboxes, make "have we fetched this?" a simple primary-key lookup. |
| **Overpass budget kept in the database** | The `upstream_calls` table is shared by every backend process and survives restarts. An in-memory counter would reset on each deploy and count per process. |
| **Incomplete results instead of errors** | When Overpass is busy or the budget is used up, the map still shows cached places and says why some are missing, rather than failing. |
| **Rate limits use the real client IP** | Requests reach FastAPI through Next.js, so the visitor's IP is in `X-Forwarded-For`. It's only trusted from `TRUSTED_PROXIES`, and read right to left, so clients can't dodge limits with a fake header. The counters are in memory for now; several backend instances would need Redis. |
| **Map data via Next.js route handlers** | The map is browser code, so it can't use Server Components to fetch on every pan. `/api/places` and `/api/geocode` forward to FastAPI server-side, so the browser still only talks to the Next.js origin and FastAPI stays private. |
| **Per-IP limits trust the edge proxy** | Next.js only fills in `X-Forwarded-For` when the request doesn't already have one, so with nothing in front of it (local dev) a client could fake its IP. In production the load balancer appends the real address, and FastAPI reads the header right to left. Per-IP limits are for fairness; the OSM usage budgets are enforced across the whole backend, so faked IPs can't push us over them. |
| **MapLibre's worker is copied into `public/`** | MapLibre parses tiles in a Web Worker that it loads from a URL it works out at runtime, which bundlers can't follow. `pnpm dev` and `pnpm build` copy the installed worker to `public/vendor/` (gitignored), and the map calls `setWorkerUrl`. |
| **Chat goes through Next.js route handlers too** | The browser streams replies from `/api/chat/...`, which forwards to FastAPI with the session cookie and passes the server-sent events straight through as they arrive. For requests that change data, the handler also refuses a foreign `Origin` header, as a second CSRF defence behind the SameSite=Lax cookie. |
| **A tiny Markdown renderer, not a library** | Chat replies only need paragraphs, lists and bold. The renderer builds React elements, so model output is always escaped (no `dangerouslySetInnerHTML`), and there's no new dependency. |
| **OSM links only if http(s)** | Anyone can edit OSM tags, so a `website` tag could be a `javascript:` URL. The details panel only links http and https addresses. |
| **Evals call the real entry point with stubbed external services** | The eval runs `chat.stream_reply` with the real model and real tools, so prompt, tool and loop changes are all measured. Only Overpass and Nominatim are replaced, by a frozen fixture and a gazetteer, so runs are reproducible and cost nothing in OSM usage. |
| **Tool use graded in code, grounding by a judge** | Whether the right tools were called with sensible arguments is checkable in code: properties, not an exact sequence. Whether an answer invents places or facts needs judgement, so a different model (Sonnet 5.5) grades it from per-claim verdicts, checked by known-negative tests. |
| **One EC2 instance running Docker Compose** | About $20 a month, against about $50 for Fargate + RDS + ALB, which matters for a demo. The production compose file mirrors development, so local and server run the same images. The trade-offs: no high availability, a short restart on deploy, and the database's backups are ours to manage (nightly `pg_dump` to S3, with a tested restore). |
| **Dev and prod stages in one Dockerfile** | Shared base image and dependency layers. `dev` bind-mounts the source with reload; `prod` copies only what runs, with no dev dependencies, as a non-root user. |
| **Migrations as a deploy step, not on startup** | Running `alembic upgrade` when a container starts races if two start at once, and hides a failed migration behind a crash loop. In production it's a one-off `migrate` service the deploy runs first. |
| **pyright over mypy** | Faster, and the same engine as VS Code's Pylance, so editor and CI agree. |
| **uv and pnpm** | Fast, lockfile-first package managers with reproducible installs. |
