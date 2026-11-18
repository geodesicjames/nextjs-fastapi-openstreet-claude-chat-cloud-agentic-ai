"""The production JSON log format."""

import json
import logging
import sys

from app.log import JsonFormatter


def test_json_log_line() -> None:
    record = logging.LogRecord(
        "app.test", logging.WARNING, __file__, 1, "Hello %s", ("world",), None
    )
    entry = json.loads(JsonFormatter().format(record))
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "app.test"
    assert entry["message"] == "Hello world"
    assert entry["time"].endswith("+00:00")


def test_exceptions_are_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("app", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    entry = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in entry["exception"]
