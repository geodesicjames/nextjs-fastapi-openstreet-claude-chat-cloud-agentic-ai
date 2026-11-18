"""Production server entry point: `python -m app.serve`.

Runs uvicorn directly (no auto-reload) with JSON logging. Settings:
- one worker: rate limits, the Overpass lock and the Nominatim spacing are
  kept in memory per process, so more workers would multiply what the app
  sends to OpenStreetMap services and weaken per-IP limits;
- proxy_headers off: uvicorn would otherwise rewrite the client address from
  X-Forwarded-For itself; the app does that in app/ratelimit.py, trusting
  only TRUSTED_PROXIES;
- no `server` header, so responses don't advertise the server software.
"""

import uvicorn

from app.log import LOGGING


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # inside the container; only reachable on the compose network
        port=8000,
        workers=1,
        proxy_headers=False,
        server_header=False,
        log_config=LOGGING,
    )


if __name__ == "__main__":
    main()
