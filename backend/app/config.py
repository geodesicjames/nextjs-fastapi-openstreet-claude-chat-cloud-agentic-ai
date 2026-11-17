"""Application settings.

pydantic-settings plays the role of Django's settings.py, with two differences:
- every setting is a typed field, validated when the app starts (a missing or
  malformed DATABASE_URL fails immediately rather than on first use);
- values come from environment variables (or a local .env file), never from
  editing a Python module per environment.
"""

from functools import lru_cache

from pydantic import PostgresDsn
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
