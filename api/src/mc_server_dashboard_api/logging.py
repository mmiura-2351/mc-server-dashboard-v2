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

# Attributes always present on a stdlib LogRecord, plus the two that
# ``Formatter.format`` sets on it (``message``, ``asctime``); anything beyond
# these on a record's __dict__ was passed by a caller via ``extra=`` and is
# surfaced in the JSON output. Built once via a throwaway record so it tracks
# the stdlib. ``color_message`` is uvicorn's ANSI-coloured copy of the message,
# a hint for its own formatter rather than a field (its lines come here:
# configure_logging).
_RESERVED_KEYS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"taskName", "message", "asctime", "color_message"}

# C0 (CR, LF and TAB included), DEL and C1 controls, the line and paragraph
# separators (some readers break a line on them) and the bidi controls (a
# bidi-aware terminal reorders the rest of the line), written as visible escapes
# the way ``repr`` writes them. A traceback or stack keeps its line breaks.
_ESCAPED = (
    *range(0x20),
    *range(0x7F, 0xA0),
    0x061C,
    0x200E,
    0x200F,
    0x2028,
    0x2029,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
)
_LINE_ESCAPES = {
    code: f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}" for code in _ESCAPED
} | {ord("\t"): "\\t", ord("\n"): "\\n", ord("\r"): "\\r"}
_BLOCK_ESCAPES = {
    code: escape for code, escape in _LINE_ESCAPES.items() if code != ord("\n")
}


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
                payload[key] = _json_value(value)
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
        # An extra JSON cannot encode is written as its ``str`` rather than
        # losing the line: uvicorn's lines come here (configure_logging), and its
        # ``websockets`` backend attaches the connection object itself.
        return json.dumps(payload, default=str)


# What rendering an extra raises when it cannot be written: a dict with a key
# JSON has no form for (TypeError), a circular container (ValueError), or a
# weakref proxy whose object is gone (ReferenceError, from ``str``). Such an
# extra is written as its ``repr`` rather than losing the line.
_UNRENDERABLE = (TypeError, ValueError, ReferenceError)


def _json_value(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
    except _UNRENDERABLE:
        return repr(value)
    return value


class TextFormatter(logging.Formatter):
    """Render a log record as ``LEVEL logger message key=value ...``.

    The human-readable counterpart of :class:`JsonFormatter`, carrying the same
    information: caller-supplied ``extra=`` fields and the correlation ID follow
    the message. Without them a structured warning that names its subject only
    in its fields (a command's ``kind`` and ``server_id``) says nothing in this
    format (issue #3363). A value with a space or other ambiguity is ``repr``-ed.

    No control character, line or paragraph separator, or bidi control is
    written raw (issue #3928): a request path or a filename in a message could
    otherwise put a terminal escape sequence, a line break that fakes a record,
    or a reordering of the line into the log. The message line escapes every
    one, so a record starts exactly one line; a traceback keeps its line breaks.
    """

    def __init__(self) -> None:
        super().__init__("%(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        # The message line has no raw line break (formatMessage), so the first
        # one starts the traceback / stack. That is escaped here rather than in
        # formatException: a handler that formatted the record first caches the
        # traceback text on it, and every later handler appends that text.
        line, sep, block = super().format(record).partition("\n")
        return f"{line}{sep}{block.translate(_BLOCK_ESCAPES)}"

    def formatMessage(self, record: logging.LogRecord) -> str:
        fields = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_KEYS
        }
        cid = correlation_id.get()
        if cid is not None:
            fields["correlation_id"] = cid
        line = super().formatMessage(record)
        if fields:
            rendered = " ".join(
                f"{key}={_text_value(value)}" for key, value in fields.items()
            )
            line = f"{line} {rendered}"
        return line.translate(_LINE_ESCAPES)


def _text_value(value: Any) -> str:
    try:
        text = str(value)
    except _UNRENDERABLE:
        return repr(value)
    return text if text and text.isprintable() and " " not in text else repr(value)


_TAG = "_mcsd_app_handler"


def configure_logging(level: str, log_format: str) -> None:
    """Install the root log handler for the configured level and format.

    Idempotent: only manages its own tagged handler.  Foreign handlers on the
    root logger (e.g. pytest *caplog* / *log_cli*) are never removed.
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

    # A warning is otherwise written to stderr by ``warnings`` itself, past this
    # handler: a plain-text line in the json format, unescaped (issue #3939).
    logging.captureWarnings(True)
    route_uvicorn_logging()


def route_uvicorn_logging() -> None:
    """Send uvicorn's server and access lines through the app handler.

    uvicorn configures its own logging before it calls the app factory
    (serve.py): its server and access loggers get handlers of their own and
    stop propagating, so their lines bypassed this handler -- the configured
    format and the control-character escaping alike (issue #3928). Every later
    ``uvicorn.Config`` built with its default ``log_config`` does the same again,
    so the lifespan calls this once more before serving (issue #3939).
    """

    for name in ("uvicorn", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
