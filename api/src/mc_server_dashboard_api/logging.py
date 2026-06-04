"""Structured JSON logging with a request-scoped correlation ID (NFR-OBS-1).

The correlation ID lives in a :class:`contextvars.ContextVar` set by the
request middleware, so every log line emitted while handling a request carries
it without threading the value through call sites. Secret values are masked by
the configuration layer before they reach a log (CONFIGURATION.md Section 3).
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        cid = correlation_id.get()
        if cid is not None:
            payload["correlation_id"] = cid
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str, log_format: str) -> None:
    """Install the root log handler for the configured level and format."""

    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
