"""Per-client-IP rate limiting for public endpoints.

A fixed one-minute window per (scope, IP), kept in memory. That is enough for
one backend process; with several (e.g. multiple containers in the cloud),
each would count separately, and the counts should move to a shared store
such as Redis. The interface (a FastAPI dependency) would stay the same.

Used on a route like this:

    @router.get("/places", dependencies=[Depends(RateLimit("places"))])
"""

import time
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings

WINDOW_SECONDS = 60


def client_ip(request: Request, settings: Settings) -> str:
    """The visitor's IP address.

    Requests from the browser arrive via Next.js, so the TCP peer is the
    Next.js server and the visitor's IP is in X-Forwarded-For. That header is
    only believed when the peer is a trusted proxy; otherwise anyone could
    send a fake one to dodge the limit. Each proxy appends to the header, so
    we walk it from the right and take the first address that isn't ours.
    """
    peer = request.client.host if request.client else "unknown"
    if not _is_trusted(peer, settings):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    for candidate in reversed([part.strip() for part in forwarded.split(",") if part.strip()]):
        if not _is_trusted(candidate, settings):
            return candidate
    return peer


def _is_trusted(address: str, settings: Settings) -> bool:
    try:
        ip = ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in settings.trusted_proxies)


@dataclass
class RateLimiter:
    """Request counts per key for the current window. One per app."""

    windows: dict[str, tuple[float, int]] = field(default_factory=dict)

    def hit(self, key: str, limit: int) -> float | None:
        """Count a request. Returns seconds until retry if over the limit."""
        now = time.monotonic()
        started, count = self.windows.get(key, (now, 0))
        if now - started >= WINDOW_SECONDS:
            started, count = now, 0
        if count >= limit:
            return WINDOW_SECONDS - (now - started)
        self.windows[key] = (started, count + 1)
        if len(self.windows) > 10_000:
            self._forget_old(now)
        return None

    def _forget_old(self, now: float) -> None:
        self.windows = {
            key: value for key, value in self.windows.items() if now - value[0] < WINDOW_SECONDS
        }


class RateLimit:
    """Dependency: 429 with Retry-After once a client exceeds the limit.

    A class with __call__ so each route can pick its own scope.
    """

    def __init__(self, scope: str) -> None:
        self.scope = scope

    def __call__(
        self, request: Request, settings: Annotated[Settings, Depends(get_settings)]
    ) -> None:
        limiter: RateLimiter = request.app.state.rate_limiter
        key = f"{self.scope}:{client_ip(request, settings)}"
        retry_after = limiter.hit(key, settings.places_rate_limit_per_minute)
        if retry_after is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests. Please slow down.",
                headers={"Retry-After": str(max(int(retry_after) + 1, 1))},
            )
