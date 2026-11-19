"""Management commands, like Django's `manage.py`.

    uv run python -m app.cli make-admin you@example.com
    uv run python -m app.cli cleanup        # delete expired tokens and sessions

In Docker Compose:  docker compose exec backend python -m app.cli make-admin ...
"""

import argparse
import asyncio

from app.db import SessionLocal, engine
from app.services.auth import delete_expired
from app.services.chat_access import make_admin


async def _make_admin(email: str) -> None:
    async with SessionLocal() as db:
        user, grant = await make_admin(db, email)
        print(f"{user.email} is an admin with chat grant {grant.id}.")
    await engine.dispose()


async def _cleanup() -> None:
    async with SessionLocal() as db:
        tokens, sessions = await delete_expired(db)
        print(f"Deleted {tokens} old login tokens and {sessions} expired sessions.")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser(
        "make-admin", help="Make a user an admin with chat access (creates the user if needed)."
    )
    admin.add_argument("email")
    commands.add_parser(
        "cleanup", help="Delete old login tokens and expired sessions (run daily on the server)."
    )
    args = parser.parse_args()

    if args.command == "make-admin":
        asyncio.run(_make_admin(args.email))
    elif args.command == "cleanup":
        asyncio.run(_cleanup())


if __name__ == "__main__":
    main()
