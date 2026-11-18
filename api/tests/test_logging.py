"""Tests for structured JSON logging and correlation-ID inclusion (NFR-OBS-1)."""

import json
import logging
import sys
from collections.abc import Iterator

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.config import (
    DatabaseSettings,
    LogSettings,
    RelaySettings,
    Settings,
)
from mc_server_dashboard_api.http_problem import (
    install_problem_handlers,
    unhandled_exception_middleware,
)
from mc_server_dashboard_api.logging import (
    _TAG,
    JsonFormatter,
    TextFormatter,
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


def test_text_formatter_renders_the_extra_fields() -> None:
    # The text format used to print only level, logger and message, dropping
    # the fields every structured WARN identifies its subject by -- e.g. the
    # control plane's over-size refusal, which names the command only in
    # ``kind`` / ``server_id`` (issue #3363).
    record = _record("refusing to send an over-limit command")
    record.kind = "ServerCommand"
    record.server_id = "srv 1"
    token = correlation_id.set("cid-123")
    try:
        line = TextFormatter().format(record)
    finally:
        correlation_id.reset(token)

    assert line == (
        "INFO test refusing to send an over-limit command "
        "kind=ServerCommand server_id='srv 1' correlation_id=cid-123"
    )


def test_text_formatter_leaves_a_record_without_fields_unchanged() -> None:
    assert TextFormatter().format(_record("hello")) == "INFO test hello"


def test_text_format_is_the_text_formatter() -> None:
    configure_logging("info", "text")
    handlers = [h for h in logging.getLogger().handlers if getattr(h, _TAG, False)]

    assert [type(h.formatter) for h in handlers] == [TextFormatter]


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
    # Strip any tagged app handler left by earlier tests (e.g.
    # test_caplog_survives_multiple_create_app calls create_app which installs
    # a JsonFormatter handler).  Without this, the probe would see the stale
    # handler and pass even if configure_logging ran AFTER the warning.
    stale = [h for h in root.handlers if getattr(h, "_mcsd_app_handler", False)]
    for h in stale:
        root.removeHandler(h)
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


# ---------------------------------------------------------------------------
# No raw control character reaches a log line (issue #3928): a crafted URL path
# put ESC / C1 sequences into the problem handlers' ERROR line, which a terminal
# reading the log would execute.
# ---------------------------------------------------------------------------


def _is_control(ch: str) -> bool:
    """C0 (CR, LF and TAB included), DEL or C1."""

    return ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0


def _raised(message: str) -> ValueError:
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


_CONTROLS = [
    pytest.param("\x00", "\\x00", id="c0-nul"),
    pytest.param("\x1b[2J", "\\x1b[2J", id="c0-esc"),
    pytest.param("\x1f", "\\x1f", id="c0-last"),
    pytest.param("\t", "\\t", id="tab"),
    pytest.param("\r", "\\r", id="cr"),
    pytest.param("\x7f", "\\x7f", id="del"),
    pytest.param("\x80", "\\x80", id="c1-first"),
    pytest.param("\x85", "\\x85", id="c1-nel"),
    pytest.param("\x9b", "\\x9b", id="c1-csi"),
    pytest.param("\x9f", "\\x9f", id="c1-last"),
    pytest.param(" é\xa0~", " é\xa0~", id="printable-kept"),
]


@pytest.mark.parametrize(
    ("raw", "escaped"), [*_CONTROLS, pytest.param("\n", "\\n", id="lf")]
)
def test_text_formatter_escapes_controls_on_the_message_line(
    raw: str, escaped: str
) -> None:
    record = _record("serving %s")
    record.args = (f"/a{raw}b",)

    assert TextFormatter().format(record) == f"INFO test serving /a{escaped}b"


def test_text_formatter_escapes_controls_in_the_fields() -> None:
    # A str field is repr-ed already; an object whose repr carries the raw
    # character is not.
    class _Raw:
        def __repr__(self) -> str:
            return "<\x1b[31m\x85>"

    record = _record("x")
    record.subject = _Raw()

    assert TextFormatter().format(record) == "INFO test x subject=<\\x1b[31m\\x85>"


@pytest.mark.parametrize(("raw", "escaped"), _CONTROLS)
def test_text_formatter_escapes_controls_in_a_traceback_but_keeps_its_lines(
    raw: str, escaped: str
) -> None:
    exc = _raised(f"bad{raw}name")
    record = _record("failed")
    record.exc_info = (type(exc), exc, exc.__traceback__)

    first, *traceback_lines = TextFormatter().format(record).split("\n")

    assert first == "INFO test failed"
    assert traceback_lines[0] == "Traceback (most recent call last):"
    assert traceback_lines[-1] == f"ValueError: bad{escaped}name"


def test_text_formatter_escapes_a_traceback_another_handler_rendered_first() -> None:
    # ``Formatter.format`` caches the traceback text on the record, so a handler
    # that formatted the record first decides what every later one appends.
    exc = _raised("bad\x1bname")
    record = _record("failed")
    record.exc_info = (type(exc), exc, exc.__traceback__)
    logging.Formatter().format(record)

    assert TextFormatter().format(record).endswith("\nValueError: bad\\x1bname")


def test_text_formatter_escapes_controls_in_the_stack() -> None:
    record = _record("here")
    record.stack_info = "Stack (most recent call last):\n  frame\x1b[2J\x85"

    assert TextFormatter().format(record) == (
        "INFO test here\nStack (most recent call last):\n  frame\\x1b[2J\\x85"
    )


def test_json_formatter_writes_no_raw_control_character() -> None:
    raw = "\x00\x1b[2J\t\r\n\x7f\x85\x9b"
    exc = _raised(raw)
    record = _record("serving %s")
    record.args = (raw,)
    record.subject = raw
    record.exc_info = (type(exc), exc, exc.__traceback__)

    line = JsonFormatter().format(record)

    assert [ch for ch in line if _is_control(ch)] == []
    parsed = json.loads(line)
    assert parsed["message"] == f"serving {raw}"
    assert parsed["subject"] == raw
    assert parsed["exception"].endswith(f"ValueError: {raw}")


def test_a_crafted_path_reaches_the_problem_error_line_escaped() -> None:
    # The whole path from the issue: a request on a route that answers 500.
    # ``urlsplit`` drops TAB, CR and LF from ``request.url.path``, so the path
    # can carry ESC, DEL and C1 only; the formatter tests above cover the rest.
    app = FastAPI()
    install_problem_handlers(app)
    app.middleware("http")(unhandled_exception_middleware)

    @app.get("/boom/{name}")
    def boom(name: str) -> None:  # noqa: ARG001 — the path is the payload
        raise RuntimeError("kaboom")

    handler = _CapturingHandler()
    handler.setFormatter(TextFormatter())
    problem_logger = logging.getLogger("mc_server_dashboard_api.http_problem")
    problem_logger.addHandler(handler)
    try:
        resp = TestClient(app, raise_server_exceptions=False).get(
            "/boom/%1B%5B2J%7F%C2%85%C2%9B"
        )
    finally:
        problem_logger.removeHandler(handler)

    assert resp.status_code == 500
    [line] = handler.lines
    assert line.split("\n")[0] == (
        "ERROR mc_server_dashboard_api.http_problem unhandled exception serving "
        "GET /boom/\\x1b[2J\\x7f\\x85\\x9b"
    )
    assert [ch for ch in line if _is_control(ch) and ch != "\n"] == []


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Put back the loggers these tests reconfigure process-wide."""

    loggers = [logging.getLogger(n) for n in ("", "uvicorn", "uvicorn.access")]
    saved = [(lg, lg.handlers[:], lg.propagate, lg.level) for lg in loggers]
    yield
    for lg, handlers, propagate, level in saved:
        lg.handlers[:] = handlers
        lg.propagate = propagate
        lg.setLevel(level)


@pytest.mark.usefixtures("_restore_logging")
def test_uvicorn_lines_go_through_the_app_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # uvicorn applies its own logging config before it calls the app factory
    # (serve.py), giving its error and access loggers handlers of their own.
    uvicorn.Config(FastAPI())
    configure_logging("info", "text")

    logging.getLogger("uvicorn.error").error("bad \x1b[2J")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/x", "1.1", 200
    )
    # uvicorn's ANSI-coloured copy of the message is no field of the line.
    logging.getLogger("uvicorn.error").info(
        "Started server process [%d]",
        42,
        extra={"color_message": "Started server process [\x1b[36m%d\x1b[0m]"},
    )

    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.splitlines() == [
        "ERROR uvicorn.error bad \\x1b[2J",
        'INFO uvicorn.access 127.0.0.1:5000 - "GET /x HTTP/1.1" 200',
        "INFO uvicorn.error Started server process [42]",
    ]
