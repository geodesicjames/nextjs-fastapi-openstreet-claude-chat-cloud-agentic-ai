"""Application settings.

pydantic-settings plays the role of Django's settings.py, with two differences:
- every setting is a typed field, validated when the app starts (a missing or
  malformed DATABASE_URL fails immediately rather than on first use);
- values come from environment variables (or a local .env file), never from
  editing a Python module per environment.
"""

from decimal import Decimal
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
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

    # --- Email ------------------------------------------------------------------
    # console: log emails (development). ses: send through Amazon SES, using
    # the server's AWS credentials (its instance role).
    email_backend: Literal["console", "ses"] = "console"
    # Sender address; must be verified in SES. Required when email_backend=ses.
    email_from: str = ""
    # AWS region for SES. On the server it comes from Parameter Store.
    aws_region: str | None = None

    # --- Chat access ----------------------------------------------------------
    # invite_only: chat needs an active ChatGrant. open: any signed-in user can
    # chat, so opening up later is a config change rather than a code change.
    chat_mode: Literal["invite_only", "open"] = "invite_only"
    # Spend per user per UTC day (estimated from token usage), for grants
    # without their own budget and for everyone in open mode.
    chat_daily_budget_usd: Decimal = Field(default=Decimal("1.00"), gt=0)

    # --- Claude chat ----------------------------------------------------------
    # Backend only: never exposed to the browser. Use a key dedicated to this
    # project, with a spend limit set in the Claude Console.
    anthropic_api_key: SecretStr | None = None
    # The model the app's chat calls (not related to tools used to build it).
    # Must have prices in app/services/pricing.py; checked at startup.
    chat_model: str = "claude-haiku-5-5"
    # How much the model reasons before answering; "low" suits chat.
    chat_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    # Per-call output cap (thinking counts towards it). Every call sets it.
    chat_max_tokens: int = Field(default=16_000, gt=0)
    # Longest user message accepted, in characters.
    chat_max_message_chars: int = Field(default=2_000, gt=0)
    # Most tool-using steps (model calls that ask for tools) per user message.
    # After that, the model must answer with what it has.
    chat_max_tool_steps: int = Field(default=6, gt=0)
    # --- Agent (trip planner) ---------------------------------------------------
    # Uses the chat grant and daily budget; these limit each run on top.
    # Must have prices in app/services/pricing.py, like CHAT_MODEL.
    agent_model: str = "claude-haiku-5-5"
    # Planning benefits from more reasoning than a chat reply.
    agent_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    agent_max_tokens: int = Field(default=16_000, gt=0)
    # Model calls per run, across every revision. The last one is told to
    # submit its plan; a run that still hasn't is stopped.
    agent_max_model_calls: int = Field(default=12, ge=2)
    # Estimated spend per run, checked before every model call.
    agent_max_cost_usd: Decimal = Field(default=Decimal("0.20"), gt=0)
    # Wall-clock time per leg (from start, or from a revision request, until
    # the run pauses for approval or ends).
    agent_max_seconds: int = Field(default=300, gt=0)
    # A "running" run whose heartbeat is older than this was interrupted
    # (e.g. by a restart) and may be picked up again.
    agent_stale_after_seconds: int = Field(default=300, gt=0)

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

    # Nominatim (place-name search). Usage policy, which this app follows:
    # https://operations.osmfoundation.org/policies/nominatim/
    # At most 1 request per second across the whole app (enforced in code),
    # results cached, no search-as-you-type.
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    nominatim_timeout_seconds: int = 10
    # A backstop against abuse spread across many IPs; the policy itself is
    # about request rate, and real use should stay far below this.
    nominatim_daily_query_budget: int = 1000
    geocode_cache_days: int = 30

    # --- Error reporting ---------------------------------------------------------
    # Sentry project DSN for the backend; unset (or empty) means Sentry is off.
    sentry_dsn: str | None = None
    # The deployed commit, set by the deploy; tags errors with the release.
    sentry_release: str | None = None

    # --- Rate limiting ----------------------------------------------------------
    # Requests per minute per client IP on the public map endpoints.
    places_rate_limit_per_minute: int = 60
    # Searches are typed by hand, so far fewer are needed than map pans.
    geocode_rate_limit_per_minute: int = 20
    # Peers allowed to tell us the real client IP via X-Forwarded-For (the
    # Next.js server). Anyone else's X-Forwarded-For is ignored, so clients
    # can't dodge rate limits by sending a fake header.
    trusted_proxies: list[IPv4Network | IPv6Network] = Field(
        default_factory=lambda: [IPv4Network("127.0.0.1/32"), IPv6Network("::1/128")]
    )

    @field_validator("anthropic_api_key", "sentry_dsn", "sentry_release", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        # `ANTHROPIC_API_KEY=` (empty, as in .env.example) means "not configured".
        return None if value == "" else value

    @field_validator("chat_model", "agent_model")
    @classmethod
    def _model_has_prices(cls, value: str) -> str:
        # Imported here: pricing is a leaf module, but keep config import-light.
        from app.services.pricing import PRICES

        if value not in PRICES:
            raise ValueError(
                f"no prices for {value!r}; add it to app/services/pricing.py "
                f"(known: {', '.join(sorted(PRICES))})"
            )
        return value

    @model_validator(mode="after")
    def _ses_needs_sender(self) -> "Settings":
        if self.email_backend == "ses" and not self.email_from:
            raise ValueError("EMAIL_FROM is required when EMAIL_BACKEND=ses")
        return self

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
