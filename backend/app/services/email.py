"""Outgoing email.

Endpoints depend on the `EmailSender` interface, not a concrete provider, so a
real sender (SES, Resend, ...) can be added at deploy time, and tests swap in
a fake via `app.dependency_overrides[get_email_sender]`.

In development, `ConsoleEmailSender` logs the message, like Django's console
email backend: the sign-in link shows up in `docker compose logs backend`.
"""

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send(self, *, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    async def send(self, *, to: str, subject: str, body: str) -> None:
        logger.info("Email to %s\nSubject: %s\n\n%s", to, subject, body)


def get_email_sender() -> EmailSender:
    return ConsoleEmailSender()
