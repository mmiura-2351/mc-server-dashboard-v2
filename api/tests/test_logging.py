"""Tests for structured JSON logging and correlation-ID inclusion (NFR-OBS-1)."""

import json
import logging
import sys

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.config import (
    DatabaseSettings,
    LogSettings,
    RelaySettings,
    Settings,
)
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


# ---------------------------------------------------------------------------
# Parametrized regression: every exc_info shape must produce valid JSON
# and never raise.  The BaseException-instance case must also carry the
# traceback (i.e. the 'exception' key must be present in the payload).
# ---------------------------------------------------------------------------


def _make_exc_with_tb() -> BaseException:
    """Return a live ValueError with __traceback__ populated."""
    try:
        raise ValueError("instance-exc")
    except ValueError as exc:
        return exc


def _make_exc_tuple() -> tuple[object, ...]:
    """Return a real (type, value, tb) from sys.exc_info()."""
    try:
        raise TypeError("tuple-exc")
    except TypeError:
        return sys.exc_info()


@pytest.mark.parametrize(
    "ei,expect_exception_key",
    [
        (None, False),
        (False, False),
        ((None, None, None), False),
        pytest.param(True, True, id="bool-true-active"),
        pytest.param(True, False, id="bool-true-no-active"),
        pytest.param("tuple", True, id="real-tuple"),
        pytest.param("instance", True, id="baseexception-instance"),
    ],
)
def test_exc_info_shapes_produce_valid_json(
    ei: object, expect_exception_key: bool
) -> None:
    """Every exc_info shape must serialise to valid JSON without raising."""
    # Resolve sentinels to real objects before any active-exception context.
    exc_with_tb = _make_exc_with_tb()
    exc_tuple = _make_exc_tuple()

    if ei == "tuple":
        ei = exc_tuple
    elif ei == "instance":
        ei = exc_with_tb

    # For the bool-true-active variant we need an active exception context.
    if expect_exception_key and ei is True:
        try:
            raise RuntimeError("active")
        except RuntimeError:
            record = _record_with_exc_info("msg", True)
            line = JsonFormatter().format(record)
    else:
        # Ensure no active exception for the no-active variant.
        assert sys.exc_info() == (None, None, None) or ei is not True
        record = _record_with_exc_info("msg", ei)
        line = JsonFormatter().format(record)

    parsed = json.loads(line)  # must not raise

    if expect_exception_key:
        assert "exception" in parsed, f"expected 'exception' key for ei={ei!r}"
    # For the instance case, verify the traceback text is present.
    if ei is exc_with_tb:
        assert "instance-exc" in parsed["exception"]
        assert "Traceback" in parsed["exception"]


# ---------------------------------------------------------------------------
# Regression: configure_logging must not detach foreign handlers (#1759)
# ---------------------------------------------------------------------------


def test_configure_logging_preserves_foreign_handlers() -> None:
    """Foreign handlers (e.g. pytest caplog) must survive configure_logging."""
    root = logging.getLogger()
    foreign = logging.StreamHandler()
    root.addHandler(foreign)
    try:
        configure_logging("info", "json")
        assert foreign in root.handlers, "foreign handler removed by configure_logging"
    finally:
        root.removeHandler(foreign)


def test_configure_logging_is_idempotent() -> None:
    """Repeated calls must not duplicate the application handler."""
    root = logging.getLogger()
    configure_logging("info", "json")
    after_first = len(root.handlers)
    configure_logging("debug", "text")
    after_second = len(root.handlers)
    # The second call replaces the handler, not adds a second one.
    assert after_second == after_first


def test_caplog_survives_multiple_create_app(caplog: pytest.LogCaptureFixture) -> None:
    """caplog must still capture app logs after multiple create_app() builds."""
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://u:s@db/app")
    )
    # Build the app twice — the second build previously detached caplog.
    create_app(settings)
    create_app(settings)

    logger = logging.getLogger("test.caplog.survival")
    with caplog.at_level(logging.INFO, logger="test.caplog.survival"):
        logger.info("visible-after-rebuild")

    assert "visible-after-rebuild" in caplog.text


# ---------------------------------------------------------------------------
# Regression: startup config warnings must use the configured JSON format
# (issue #1992). Before the fix, configure_logging ran after the warnings,
# so they fell through to logging.lastResort as plain text.
# ---------------------------------------------------------------------------


class _JsonProbeHandler(logging.Handler):
    """Handler that records whether a ``JsonFormatter`` sibling exists on the
    root logger at the moment each record is emitted."""

    def __init__(self) -> None:
        super().__init__()
        self.had_json_formatter: dict[str, bool] = {}

    def emit(self, record: logging.LogRecord) -> None:
        root = logging.getLogger()
        self.had_json_formatter[record.getMessage()] = any(
            isinstance(h.formatter, JsonFormatter)
            for h in root.handlers
            if h is not self
        )


def test_startup_warnings_use_configured_json_format() -> None:
    """Config warnings emitted during create_app must go through the
    configured JSON formatter, not bypass it via logging.lastResort."""
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://u:s@db/app"),
        log=LogSettings(format="json"),
        relay=RelaySettings(bedrock_enabled=True, enabled=False),
    )
    probe = _JsonProbeHandler()
    probe.setLevel(logging.WARNING)
    root = logging.getLogger()
    root.addHandler(probe)
    try:
        create_app(settings)
    finally:
        root.removeHandler(probe)

    # The bedrock_enabled warning must have been emitted while a
    # JsonFormatter handler was already installed on the root logger.
    bedrock_msgs = [
        msg for msg in probe.had_json_formatter if "relay.bedrock_enabled" in msg
    ]
    assert bedrock_msgs, "bedrock_enabled warning was not emitted"
    for msg in bedrock_msgs:
        assert probe.had_json_formatter[msg], (
            "relay.bedrock_enabled warning was emitted before "
            "configure_logging installed the JsonFormatter"
        )
