"""Logging setup for production: one JSON object per line on stdout.

Log collectors (CloudWatch, Loki, ...) can search and filter JSON fields,
which plain text lines make hard. Development keeps the readable text format
set up in app/main.py.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


# A logging.config.dictConfig configuration, also passed to uvicorn so its
# own loggers (startup, access log) use the same JSON format.
LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": JsonFormatter}},
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stdout",
        }
    },
    "root": {"handlers": ["stdout"], "level": "INFO"},
    "loggers": {
        name: {"handlers": ["stdout"], "level": "INFO", "propagate": False}
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    },
}
