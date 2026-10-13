"""Structured JSON logging with a request-scoped correlation ID (NFR-OBS-1).

The correlation ID lives in a :class:`contextvars.ContextVar` set by the
request middleware, so every log line emitted while handling a request carries
it without threading the value through call sites. Secret values are masked by
the configuration layer before they reach a log (CONFIGURATION.md Section 3).
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Attributes always present on a stdlib LogRecord; anything beyond these on a
# record's __dict__ was passed by a caller via ``extra=`` and is surfaced in the
# JSON output. Built once via a throwaway record so it tracks the stdlib.
_RESERVED_KEYS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"taskName"}


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object.

    Caller-supplied ``extra=`` fields are merged into the output. Masking is the
    caller's responsibility: pass pre-masked data (e.g. ``Settings.masked_dump``)
    — this formatter does not and cannot redact secrets it is handed.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_KEYS:
                payload[key] = value
        cid = correlation_id.get()
        if cid is not None:
            payload["correlation_id"] = cid
        ei = record.exc_info
        if not isinstance(ei, tuple):
            if isinstance(ei, BaseException):
                # Logger._log converts a BaseException instance to a tuple; mirror
                # that here so the traceback attached to the instance is preserved.
                ei = (type(ei), ei, ei.__traceback__)
            else:
                # Callers such as asyncio's call_exception_handler set exc_info=True
                # directly on the record (skipping Logger.makeRecord, which normally
                # normalises True to a sys.exc_info() tuple).  Resolve it here so
                # formatException always receives a proper (type, value, tb) triple.
                ei = sys.exc_info() if ei else None
        if isinstance(ei, tuple) and ei[0] is not None:
            payload["exception"] = self.formatException(ei)
        return json.dumps(payload)


class TextFormatter(logging.Formatter):
    """Render a log record as ``LEVEL logger message key=value ...``.

    The human-readable counterpart of :class:`JsonFormatter`, carrying the same
    information: caller-supplied ``extra=`` fields and the correlation ID follow
    the message. Without them a structured warning that names its subject only
    in its fields (a command's ``kind`` and ``server_id``) says nothing in this
    format (issue #3363). A value with a space or other ambiguity is ``repr``-ed.
    """

    def __init__(self) -> None:
        super().__init__("%(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        fields = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_KEYS
        }
        cid = correlation_id.get()
        if cid is not None:
            fields["correlation_id"] = cid
        if not fields:
            return super().format(record)
        # The fields belong to the message line, ahead of any traceback.
        first, sep, rest = super().format(record).partition("\n")
        rendered = " ".join(
            f"{key}={_text_value(value)}" for key, value in fields.items()
        )
        return f"{first} {rendered}{sep}{rest}"


def _text_value(value: Any) -> str:
    text = str(value)
    return text if text and text.isprintable() and " " not in text else repr(value)


_TAG = "_mcsd_app_handler"


def configure_logging(level: str, log_format: str) -> None:
    """Install the root log handler for the configured level and format.

    Idempotent: only manages its own tagged handler.  Foreign handlers (e.g.
    pytest *caplog* / *log_cli*) are never removed.
    """

    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter())
    setattr(handler, _TAG, True)

    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if not getattr(h, _TAG, False)]
    root.addHandler(handler)
    root.setLevel(level.upper())
