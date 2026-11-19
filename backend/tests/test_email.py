"""Email senders and how one is chosen.

The SES tests use botocore's Stubber, which checks each request against the
real SES API model and answers it locally: no network and no AWS account.
"""

import logging

import boto3
import pytest
from botocore.stub import Stubber
from pydantic import ValidationError

from app.config import Settings
from app.services.email import ConsoleEmailSender, SESEmailSender, get_email_sender


def make_settings(**overrides: object) -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        database_url="postgresql+asyncpg://u:p@localhost/db",  # pyright: ignore[reportArgumentType]
        **overrides,  # pyright: ignore[reportArgumentType]
    )


@pytest.fixture
def ses_client():
    # Dummy credentials: the Stubber answers before anything is sent.
    return boto3.client(
        "sesv2",
        region_name="eu-west-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


async def test_ses_sends_a_plain_text_email(ses_client, caplog: pytest.LogCaptureFixture) -> None:
    sender = SESEmailSender(sender="app@example.com", client=ses_client)
    with Stubber(ses_client) as stub:
        stub.add_response(
            "send_email",
            {"MessageId": "msg-123"},
            expected_params={
                "FromEmailAddress": "app@example.com",
                "Destination": {"ToAddresses": ["ada@example.com"]},
                "Content": {
                    "Simple": {
                        "Subject": {"Data": "Your sign-in link", "Charset": "UTF-8"},
                        "Body": {"Text": {"Data": "Click: https://x/y", "Charset": "UTF-8"}},
                    }
                },
            },
        )
        with caplog.at_level(logging.INFO, logger="app.services.email"):
            await sender.send(
                to="ada@example.com", subject="Your sign-in link", body="Click: https://x/y"
            )
        stub.assert_no_pending_responses()
    assert "SES sent msg-123" in caplog.text


async def test_ses_failure_is_logged_not_raised(
    ses_client, caplog: pytest.LogCaptureFixture
) -> None:
    sender = SESEmailSender(sender="app@example.com", client=ses_client)
    with Stubber(ses_client) as stub:
        stub.add_client_error(
            "send_email",
            service_error_code="MessageRejected",
            service_message="Email address is not verified.",
        )
        # Runs as a background task after the response: must not raise.
        await sender.send(to="new@example.com", subject="Hi", body="Hello")
    assert "SES send failed" in caplog.text
    assert "not verified" in caplog.text
    # The recipient's address stays out of the logs.
    assert "new@example.com" not in caplog.text


def test_console_is_the_default() -> None:
    assert isinstance(get_email_sender(make_settings()), ConsoleEmailSender)


def test_ses_backend_selects_ses() -> None:
    settings = make_settings(
        email_backend="ses", email_from="app@example.com", aws_region="eu-west-2"
    )
    sender = get_email_sender(settings)
    assert isinstance(sender, SESEmailSender)
    assert sender.sender == "app@example.com"
    assert sender.client.meta.region_name == "eu-west-2"
    # One sender (and boto3 client) is reused across requests.
    assert get_email_sender(settings) is sender


def test_ses_backend_requires_a_sender() -> None:
    with pytest.raises(ValidationError, match="EMAIL_FROM is required"):
        make_settings(email_backend="ses", email_from="")
