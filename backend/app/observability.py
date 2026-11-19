"""Error reporting to Sentry.

Off unless SENTRY_DSN is set (so local development and tests send nothing).
When on, Sentry receives unhandled exceptions, 5xx responses and
`logger.error` / `logger.exception` records, with the stack trace and the
request's method and path.

Privacy: it gets no personal data. Sentry's defaults would attach user
identity, IP addresses, cookies, headers, request bodies, query strings,
local variables from each stack frame (which can hold emails and tokens) and
the prompts and replies of Claude calls. Everything below turns those off.
Performance tracing stays off too (no traces_sample_rate): errors only.
"""

from typing import Any

import sentry_sdk

from app.config import Settings

OFF = {"mode": "off"}


def sentry_options(settings: Settings) -> dict[str, Any] | None:
    """The sentry_sdk.init() arguments, or None if Sentry isn't configured."""
    if settings.sentry_dsn is None:
        return None
    return {
        "dsn": settings.sentry_dsn,
        "environment": settings.environment,
        # The deploy sets SENTRY_RELEASE to the commit SHA; None lets the SDK
        # read that variable itself.
        "release": settings.sentry_release,
        "data_collection": {
            "user_info": False,
            "cookies": OFF,
            "http_headers": {"request": OFF},
            "http_bodies": [],
            "url_query_params": OFF,
            "gen_ai": {"inputs": False, "outputs": False},
            "database_query_data": False,
            "stack_frame_variables": False,
        },
    }


def init_sentry(settings: Settings) -> bool:
    """Start the Sentry SDK if configured; return whether it was started."""
    options = sentry_options(settings)
    if options is None:
        return False
    sentry_sdk.init(**options)
    return True
