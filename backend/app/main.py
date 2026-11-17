"""FastAPI application entry point.

`create_app()` is an app factory: it builds and configures a fresh FastAPI
instance. Django builds its app from settings.INSTALLED_APPS and ROOT_URLCONF;
here we wire routers and middleware explicitly in code.

Run locally with:  uv run fastapi dev app/main.py
"""

import logging

from fastapi import FastAPI

from app.routers import auth, health


def create_app() -> FastAPI:
    # Send our own `app.*` loggers (e.g. the dev console email) to stderr at
    # INFO. Uvicorn configures only its own loggers.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")

    app = FastAPI(title="Places API", version="0.1.0")

    # No CORS middleware: browsers never call this API directly. They talk
    # only to Next.js, which calls FastAPI server-side and forwards the
    # session cookie, so every browser request is same-origin.

    app.include_router(health.router)
    app.include_router(auth.router)

    return app


app = create_app()
