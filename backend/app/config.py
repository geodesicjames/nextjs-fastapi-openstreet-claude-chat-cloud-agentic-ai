"""Application settings.

pydantic-settings plays the role of Django's settings.py, with two differences:
- every setting is a typed field, validated when the app starts (a missing or
  malformed DATABASE_URL fails immediately rather than on first use);
- values come from environment variables (or a local .env file), never from
  editing a Python module per environment.
"""

from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network
from typing import Literal

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # The repo-root .env is shared by Compose, backend and frontend; a
        # backend/.env (if present) overrides it. Real environment variables
        # always win over both.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",  # the shared .env also holds frontend/compose variables
    )

    environment: str = "development"

    # Must use the asyncpg driver, e.g.
    # postgresql+asyncpg://app:app@localhost:5432/app
    database_url: PostgresDsn

    # --- Auth ---------------------------------------------------------------
    # Public URL of the Next.js app; magic links point at its /auth/verify page.
    frontend_url: str = "http://localhost:3000"
    login_link_lifetime_minutes: int = 30
    # At most this many links per email address per hour; extra requests are
    # silently dropped (the response is the same, so it reveals nothing).
    login_link_max_per_hour: int = 5
    session_lifetime_days: int = 30
    session_cookie_name: str = "session"

    # --- Chat access ----------------------------------------------------------
    # invite_only: chat needs an active ChatGrant. open: any signed-in user can
    # chat, so opening up later is a config change rather than a code change.
    chat_mode: Literal["invite_only", "open"] = "invite_only"
    # Tokens (input + output) per user per UTC day, for grants without their
    # own limit and for everyone in open mode.
    chat_default_daily_tokens: int = Field(default=50_000, gt=0)
    # Access requests per email address per 24 hours; extras are silently
    # dropped (same response, so the form can't be used to spam the owner).
    access_request_max_per_day: int = 3

    # --- Map data (OpenStreetMap services) --------------------------------------
    # Public OSM services require an identifying User-Agent (not a library
    # default). Put a URL or email people can reach you at here; it is sent
    # only to the OSM services, as part of that header.
    osm_contact: str = ""
    # A community-run public instance (Europe; .../api-na/interpreter for North
    # America), used because the main overpass-api.de is often at capacity.
    # It asks for an identifying User-Agent and at most 4 parallel queries per
    # IP. The budgets below follow the stricter main-instance policy anyway.
    overpass_url: str = "https://ovp.dresel.systems/api/interpreter"
    # Server-side query timeout. Our queries cover at most a few km², which
    # normally takes a second or two; the HTTP wait is this plus 5 seconds.
    overpass_timeout_seconds: int = 10
    # The public Overpass instance asks apps that use it regularly to stay
    # under ~100 queries and ~10 MB a day across all users; keep a margin.
    overpass_daily_query_budget: int = 80
    overpass_daily_mb_budget: float = 8.0
    # How long fetched places are trusted before a cell is fetched again.
    place_cache_days: int = 30

    # --- Rate limiting ----------------------------------------------------------
    # Requests per minute per client IP on the public map endpoints.
    places_rate_limit_per_minute: int = 60
    # Peers allowed to tell us the real client IP via X-Forwarded-For (the
    # Next.js server). Anyone else's X-Forwarded-For is ignored, so clients
    # can't dodge rate limits by sending a fake header.
    trusted_proxies: list[IPv4Network | IPv6Network] = Field(
        default_factory=lambda: [IPv4Network("127.0.0.1/32"), IPv6Network("::1/128")]
    )

    @property
    def user_agent(self) -> str:
        contact = f" (+{self.osm_contact})" if self.osm_contact else ""
        return f"PlacesPortfolioApp/0.1{contact}"

    @property
    def cookie_secure(self) -> bool:
        """Send the cookie over HTTPS only, everywhere except local dev."""
        return self.environment != "development"


@lru_cache
def get_settings() -> Settings:
    """Return the settings, built once and cached.

    Exposed as a function (rather than a module-level `settings` object) so it
    can be used as a FastAPI dependency and overridden in tests.
    """
    return Settings()  # pyright: ignore[reportCallIssue]  # fields come from env
