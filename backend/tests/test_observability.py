"""Sentry configuration: off by default, and no personal data when on."""

import json
from typing import Any

import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from app.config import Settings
from app.observability import init_sentry, sentry_options


def make_settings(**overrides: object) -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        database_url="postgresql+asyncpg://u:p@localhost/db",  # pyright: ignore[reportArgumentType]
        **overrides,  # pyright: ignore[reportArgumentType]
    )


DSN = "https://public@o1.ingest.sentry.io/1"


def test_off_without_a_dsn() -> None:
    assert sentry_options(make_settings()) is None
    assert sentry_options(make_settings(sentry_dsn="")) is None
    assert init_sentry(make_settings()) is False


def test_no_personal_data_and_no_tracing() -> None:
    options = sentry_options(
        make_settings(sentry_dsn=DSN, environment="production", sentry_release="abc123")
    )
    assert options is not None
    assert options["dsn"] == DSN
    assert options["environment"] == "production"
    assert options["release"] == "abc123"
    data = options["data_collection"]
    assert data["user_info"] is False
    assert data["stack_frame_variables"] is False
    assert data["http_bodies"] == []
    assert data["gen_ai"] == {"inputs": False, "outputs": False}
    for key in ("cookies", "url_query_params"):
        assert data[key] == {"mode": "off"}
    assert data["http_headers"] == {"request": {"mode": "off"}}
    # Errors only: tracing needs traces_sample_rate (or a sampler).
    assert "traces_sample_rate" not in options and "traces_sampler" not in options


class Recorder(Transport):
    """A Sentry transport that keeps events in memory instead of sending them."""

    def __init__(self, options: dict | None = None) -> None:
        super().__init__(options)
        self.events: list[Any] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            event = item.get_event()
            if event is not None:
                self.events.append(event)


def test_captured_error_carries_no_local_variables() -> None:
    """Report a real exception through the SDK and inspect what would be sent."""
    options = sentry_options(make_settings(sentry_dsn=DSN))
    assert options is not None
    recorder = Recorder()
    try:
        sentry_sdk.init(**options, transport=recorder)
        try:
            # Built at runtime, so it isn't in the source lines Sentry includes.
            email = "ada" + "@" + "example.com"
            raise ValueError(f"lookup failed for user {len(email)}")
        except ValueError as error:
            sentry_sdk.capture_exception(error)
        sentry_sdk.flush()
    finally:
        sentry_sdk.init()  # back to a disabled client for other tests

    assert len(recorder.events) == 1
    event = recorder.events[0]
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    assert frames and all("vars" not in frame for frame in frames)
    assert "ada@example.com" not in json.dumps(event)
    assert "user" not in event or not event["user"].get("ip_address")
