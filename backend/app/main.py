"""FastAPI application entry point.

`create_app()` is an app factory: it builds and configures a fresh FastAPI
instance. Django builds its app from settings.INSTALLED_APPS and ROOT_URLCONF;
here we wire routers and middleware explicitly in code.

Run locally with:  uv run fastapi dev app/main.py
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.config import get_settings
from app.deps import chat_model_for
from app.observability import init_sentry
from app.ratelimit import RateLimiter
from app.routers import (
    access_requests,
    admin,
    agent,
    auth,
    chat,
    geocode,
    health,
    places,
    saved_places,
)
from app.services.agent_worker import AgentWorker
from app.services.geocoding import NominatimClient
from app.services.overpass import OverpassClient


def create_app() -> FastAPI:
    # Send our own `app.*` loggers (e.g. the dev console email) to stderr at
    # INFO. Uvicorn configures only its own loggers.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
    # Error reporting, before anything can fail. Off unless SENTRY_DSN is set.
    init_sentry(get_settings())

    # One HTTP client (with its connection pool) for every outgoing call,
    # closed on shutdown. The lifespan function is FastAPI's startup/shutdown
    # hook: code before `yield` runs at startup, code after it at shutdown.
    http = httpx.AsyncClient()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Picks up queued runs and runs interrupted by a restart.
        sweeper = asyncio.create_task(app.state.agent_worker.run_sweeper())
        yield
        sweeper.cancel()
        await app.state.agent_worker.stop()
        await http.aclose()
        if app.state.anthropic is not None:
            await app.state.anthropic.close()

    app = FastAPI(title="Places API", version="0.1.0", lifespan=lifespan)

    # Process-wide objects, reached from dependencies via request.app.state.
    # Built here rather than in lifespan so tests (which don't run lifespan)
    # get them too, fresh for every app.
    app.state.overpass = OverpassClient(http=http)
    app.state.nominatim = NominatimClient(http=http)
    app.state.rate_limiter = RateLimiter()
    # The Anthropic client is created on first use (app/deps.py), so the app
    # starts without an API key; chat then answers 503 until one is set.
    app.state.anthropic = None
    # Drives agent runs in the background (app/services/agent_worker.py).
    app.state.agent_worker = AgentWorker(app, chat_model_for)

    # No CORS middleware: browsers never call this API directly. They talk
    # only to Next.js, which calls FastAPI server-side and forwards the
    # session cookie, so every browser request is same-origin.

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(agent.router)
    app.include_router(access_requests.router)
    app.include_router(admin.router)
    app.include_router(places.router)
    app.include_router(geocode.router)
    app.include_router(saved_places.router)

    return app


app = create_app()
