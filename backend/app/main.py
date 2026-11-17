"""FastAPI application entry point.

`create_app()` is an app factory: it builds and configures a fresh FastAPI
instance. Django builds its app from settings.INSTALLED_APPS and ROOT_URLCONF;
here we wire routers and middleware explicitly in code.

Run locally with:  uv run fastapi dev app/main.py
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import health


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="Places API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,  # needed later for the httpOnly auth cookie
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)

    return app


app = create_app()
