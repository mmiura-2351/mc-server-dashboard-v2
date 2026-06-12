"""Tests for structured JSON logging and correlation-ID inclusion (NFR-OBS-1)."""

import json
import logging
import sys

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.config import DatabaseSettings, Settings
from mc_server_dashboard_api.logging import (
    JsonFormatter,
    configure_logging,
    correlation_id,
)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_json_formatter_emits_parseable_object() -> None:
    line = JsonFormatter().format(_record("hello"))
    parsed = json.loads(line)
    assert parsed["message"] == "hello"
    assert parsed["level"] == "info"


def test_extra_fields_included_in_output() -> None:
    record = _record("x")
    record.config = {"database": {"url": "***"}}
    parsed = json.loads(JsonFormatter().format(record))
    assert parsed["config"] == {"database": {"url": "***"}}


def test_correlation_id_included_when_set() -> None:
    token = correlation_id.set("cid-123")
    try:
        parsed = json.loads(JsonFormatter().format(_record("x")))
    finally:
        correlation_id.reset(token)
    assert parsed["correlation_id"] == "cid-123"


def test_correlation_id_absent_when_unset() -> None:
    parsed = json.loads(JsonFormatter().format(_record("x")))
    assert "correlation_id" not in parsed


def test_configure_logging_sets_level_and_handler() -> None:
    configure_logging("debug", "json")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert any(isinstance(h.formatter, JsonFormatter) for h in root.handlers)


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _record_with_exc_info(message: str, exc_info: object) -> logging.LogRecord:
    r = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    r.exc_info = exc_info  # type: ignore[assignment]
    return r


def test_exc_info_bool_active_exception_includes_traceback() -> None:
    """exc_info=True while an exception is active must format without raising
    and the output must contain the traceback (not just be silently dropped)."""
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record_with_exc_info("oops", True)
        line = JsonFormatter().format(record)
    parsed = json.loads(line)
    assert "exception" in parsed
    assert "ValueError" in parsed["exception"]
    assert "boom" in parsed["exception"]


def test_exc_info_bool_no_active_exception_does_not_raise() -> None:
    """exc_info=True with no active exception must not raise and must still
    produce a valid JSON record (exception key may be absent or empty)."""
    assert sys.exc_info() == (None, None, None)  # guard: no active exception
    record = _record_with_exc_info("no-exc", True)
    line = JsonFormatter().format(record)
    json.loads(line)  # must not raise


def test_exc_info_tuple_formats_traceback_unchanged() -> None:
    """Regular (type, value, tb) tuple must still produce the traceback in output."""
    try:
        raise RuntimeError("original")
    except RuntimeError:
        ei = sys.exc_info()
        record = _record_with_exc_info("err", ei)
    line = JsonFormatter().format(record)
    parsed = json.loads(line)
    assert "exception" in parsed
    assert "RuntimeError" in parsed["exception"]
    assert "original" in parsed["exception"]


def test_startup_log_contains_masked_config() -> None:
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://u:secret@db/app")
    )
    app = create_app(settings)  # configure_logging has now reset root handlers
    handler = _CapturingHandler()
    logging.getLogger().addHandler(handler)
    try:
        with TestClient(app):  # entering runs the lifespan startup log
            pass
    finally:
        logging.getLogger().removeHandler(handler)

    startup = [json.loads(line) for line in handler.lines]
    startup = [r for r in startup if r["message"] == "api starting"]
    assert startup, "startup log line not captured"
    assert startup[0]["config"]["database"]["url"] == "***"
