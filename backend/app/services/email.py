"""Outgoing email.

Endpoints depend on the `EmailSender` interface, not a concrete provider, so
tests swap in a fake via `app.dependency_overrides[get_email_sender]`, and the
real provider is picked by the EMAIL_BACKEND setting (like Django's
EMAIL_BACKEND):

- "console" (development): logs the message, like Django's console email
  backend, so the sign-in link shows up in `docker compose logs backend`.
- "ses" (production): sends through Amazon SES, from EMAIL_FROM. AWS
  credentials come from the server's instance role; nothing is configured.

Emails are sent from BackgroundTasks, after the response has gone, so a
failure can't reach the user. Senders log failures instead of raising.
"""

import logging
from functools import lru_cache
from typing import Annotated, Any, Protocol

import boto3
from anyio import to_thread
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send(self, *, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    async def send(self, *, to: str, subject: str, body: str) -> None:
        logger.info("Email to %s\nSubject: %s\n\n%s", to, subject, body)


class SESEmailSender:
    """Plain-text email through the Amazon SES v2 API.

    boto3 is a blocking library, so each call runs in a worker thread
    (anyio.to_thread) rather than inside the event loop, where it would stall
    every other request while waiting on AWS.
    """

    def __init__(self, *, sender: str, region: str | None = None, client: Any = None) -> None:
        self.sender = sender
        # Creating a client is slow-ish, and clients are thread-safe: make one.
        self.client = client or boto3.client("sesv2", region_name=region)

    def _send_blocking(self, to: str, subject: str, body: str) -> str:
        response = self.client.send_email(
            FromEmailAddress=self.sender,
            Destination={"ToAddresses": [to]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                }
            },
        )
        return response["MessageId"]

    async def send(self, *, to: str, subject: str, body: str) -> None:
        try:
            message_id = await to_thread.run_sync(self._send_blocking, to, subject, body)
        except (ClientError, BotoCoreError):
            # E.g. an unverified recipient while SES is in its sandbox. The
            # recipient isn't logged: the error says enough to act on.
            logger.exception("SES send failed (subject %r)", subject)
            return
        logger.info("SES sent %s (subject %r)", message_id, subject)


@lru_cache
def _ses_sender(sender: str, region: str | None) -> SESEmailSender:
    return SESEmailSender(sender=sender, region=region)


def get_email_sender(settings: Annotated[Settings, Depends(get_settings)]) -> EmailSender:
    if settings.email_backend == "ses":
        return _ses_sender(settings.email_from, settings.aws_region)
    return ConsoleEmailSender()
