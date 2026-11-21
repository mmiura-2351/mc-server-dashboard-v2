"""Tests for structured JSON logging and correlation-ID inclusion (NFR-OBS-1)."""

import asyncio
import json
import logging
import re
import socket
import sys
import threading
import time
import warnings
import weakref
from collections.abc import Iterator

import httpx2
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
from mc_server_dashboard_api.observability import (
    create_observability_app,
    start_observability_listener,
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


# Line and paragraph separators (some readers break a line on them) and the
# bidi controls (a bidi-aware terminal reorders the rest of the line).
_SEPARATORS_AND_BIDI = frozenset(
    "\u061c\u200e\u200f\u2028\u2029\u202a\u202b\u202c\u202d\u202e"
    "\u2066\u2067\u2068\u2069"
)


def _is_control(ch: str) -> bool:
    """C0 (CR, LF and TAB included), DEL, C1, a separator or a bidi control."""

    return ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0 or ch in _SEPARATORS_AND_BIDI


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
    pytest.param("\u061c", "\\u061c", id="bidi-alm"),
    pytest.param("\u200e", "\\u200e", id="bidi-lrm"),
    pytest.param("\u200f", "\\u200f", id="bidi-rlm"),
    pytest.param("\u2028", "\\u2028", id="line-separator"),
    pytest.param("\u2029", "\\u2029", id="paragraph-separator"),
    pytest.param("\u202a", "\\u202a", id="bidi-lre"),
    pytest.param("\u202e", "\\u202e", id="bidi-rlo"),
    pytest.param("\u2066", "\\u2066", id="bidi-lri"),
    pytest.param("\u2069", "\\u2069", id="bidi-pdi"),
    # NBSP, the ideographic space, ZWNJ and ZWJ belong in legitimate names.
    pytest.param(
        " é\xa0~\u3000\u200c\u200d", " é\xa0~\u3000\u200c\u200d", id="printable-kept"
    ),
    pytest.param(
        "\u061b\u061d\u2027\u202f\u2065\u206a",
        "\u061b\u061d\u2027\u202f\u2065\u206a",
        id="neighbours-kept",
    ),
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
    # that formatted the record first decides what every later one appends. It
    # also leaves ``message`` and ``asctime`` on the record, which are no fields.
    exc = _raised("bad\x1bname")
    record = _record("failed")
    record.exc_info = (type(exc), exc, exc.__traceback__)
    logging.Formatter("%(asctime)s %(message)s").format(record)

    lines = TextFormatter().format(record).split("\n")

    assert lines[0] == "INFO test failed"
    assert lines[-1] == "ValueError: bad\\x1bname"


def test_text_formatter_escapes_controls_in_the_stack() -> None:
    record = _record("here")
    record.stack_info = "Stack (most recent call last):\n  frame\x1b[2J\x85"

    assert TextFormatter().format(record) == (
        "INFO test here\nStack (most recent call last):\n  frame\\x1b[2J\\x85"
    )


def test_json_formatter_writes_no_raw_control_character() -> None:
    raw = "\x00\x1b[2J\t\r\n\x7f\x85\x9b\u2028\u202e"
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


class _Connection:
    def __str__(self) -> str:
        return "conn-1"


@pytest.mark.parametrize(
    ("formatter", "expected"),
    [
        pytest.param(
            JsonFormatter(),
            '{"level": "info", "logger": "test.adapter", '
            '"message": "connection open", "websocket": "conn-1"}',
            id="json",
        ),
        pytest.param(
            TextFormatter(),
            "INFO test.adapter connection open websocket=conn-1",
            id="text",
        ),
    ],
)
def test_an_extra_json_cannot_encode_keeps_its_line(
    formatter: logging.Formatter, expected: str
) -> None:
    # uvicorn's ``websockets`` backend logs on ``uvicorn.error`` through the
    # library's LoggerAdapter, which attaches the connection object itself;
    # those lines reach these formatters since configure_logging routes uvicorn
    # here.
    connection = _Connection()
    logger = logging.getLogger("test.adapter")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _CapturingHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    try:
        adapter = logging.LoggerAdapter(
            logger, {"websocket": weakref.proxy(connection)}
        )
        adapter.info("connection open")
    finally:
        logger.removeHandler(handler)

    assert handler.lines == [expected]


def _dead_proxy() -> object:
    target = _Connection()
    proxy = weakref.proxy(target)
    del target
    return proxy


_UNENCODABLE = [
    pytest.param({(1, 2): "x"}, id="non-str-key"),
    pytest.param(_dead_proxy(), id="dead-proxy"),
]


@pytest.mark.parametrize("value", _UNENCODABLE)
def test_json_formatter_writes_an_extra_it_cannot_encode_as_its_repr(
    value: object,
) -> None:
    record = _record("x")
    record.subject = value

    assert json.loads(JsonFormatter().format(record))["subject"] == repr(value)


@pytest.mark.parametrize("value", _UNENCODABLE)
def test_text_formatter_writes_an_extra_it_cannot_encode_as_its_repr(
    value: object,
) -> None:
    record = _record("x")
    record.subject = value

    assert TextFormatter().format(record) == f"INFO test x subject={value!r}"


def test_a_crafted_path_reaches_the_problem_error_line_escaped() -> None:
    # The whole path from the issue: a request on a route that answers 500.
    # ``urlsplit`` drops TAB, CR and LF from ``request.url.path``; the path
    # carries everything else, and the formatter tests above cover those three.
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
            "/boom/%1B%5B2J%7F%C2%85%C2%9B%E2%80%A8%E2%80%AE"
        )
    finally:
        problem_logger.removeHandler(handler)

    assert resp.status_code == 500
    [line] = handler.lines
    assert line.split("\n")[0] == (
        "ERROR mc_server_dashboard_api.http_problem unhandled exception serving "
        "GET /boom/\\x1b[2J\\x7f\\x85\\x9b\\u2028\\u202e"
    )
    assert [ch for ch in line if _is_control(ch) and ch != "\n"] == []


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Put back the loggers these tests reconfigure process-wide."""

    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    loggers = [logging.getLogger(n) for n in names]
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


# ---------------------------------------------------------------------------
# Nothing built after configure_logging takes uvicorn's lines away from the app
# handler (issue #3939). uvicorn's ``Config`` reconfigures its loggers
# process-wide when it is built, so a second server in the process silenced the
# public API's access log.
# ---------------------------------------------------------------------------


_ACCESS_ARGS = ('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/x", "1.1", 200)
_ACCESS_LINE = 'INFO uvicorn.access 127.0.0.1:5000 - "GET /x HTTP/1.1" 200'


def _serve_main_app(
    monkeypatch: pytest.MonkeyPatch, paths: list[tuple[str, str]]
) -> None:
    """Serve ``create_app`` under a real uvicorn and GET each (listener, path).

    Built the way ``serve.py`` builds it: uvicorn's default ``Config`` (its own
    ``dictConfig``) first, then the app factory. ``listener`` is ``"api"`` or
    ``"metrics"``; the metrics listener is enabled on a loopback port.
    """

    monkeypatch.setenv("MCD_API_LOG__FORMAT", "text")
    monkeypatch.setenv("MCD_API_METRICS__ENABLED", "true")
    monkeypatch.setenv("MCD_API_METRICS__HOST", "127.0.0.1")
    monkeypatch.setenv("MCD_API_METRICS__PORT", "0")
    apps: list[FastAPI] = []

    def factory() -> FastAPI:
        apps.append(create_app())
        return apps[0]

    sock = socket.create_server(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(factory, factory=True))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert thread.is_alive(), "the API server did not start"
            assert time.monotonic() < deadline, "the API server did not start"
            time.sleep(0.01)
        ports = {
            "api": sock.getsockname()[1],
            "metrics": apps[0].state.metrics_listener.port,
        }
        for listener, path in paths:
            httpx2.get(f"http://127.0.0.1:{ports[listener]}{path}", timeout=10.0)
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()


@pytest.mark.usefixtures("_restore_logging")
def test_the_api_access_log_survives_the_metrics_listener(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve_main_app(monkeypatch, [("api", "/api/healthz")])

    access = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("INFO uvicorn.access ")
    ]
    assert len(access) == 1
    assert re.fullmatch(
        r'INFO uvicorn\.access 127\.0\.0\.1:\d+ - "GET /api/healthz HTTP/1\.1" '
        r"(200|503)",
        access[0],
    )


@pytest.mark.usefixtures("_restore_logging")
def test_a_scrape_writes_no_access_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A scrape every few seconds would otherwise write an access line every few
    # seconds, forever. The API's own request is the control.
    _serve_main_app(monkeypatch, [("metrics", "/metrics"), ("api", "/api/healthz")])

    access = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("INFO uvicorn.access ")
    ]
    assert len(access) == 1
    assert '"GET /api/healthz HTTP/1.1"' in access[0]


@pytest.mark.usefixtures("_restore_logging")
def test_the_metrics_listener_leaves_uvicorn_logging_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info", "text")

    async def _run() -> None:
        listener = start_observability_listener(
            create_observability_app(), host="127.0.0.1", port=0
        )
        assert listener is not None
        logging.getLogger("uvicorn.error").error("bad")
        logging.getLogger("uvicorn.access").info(*_ACCESS_ARGS)
        await listener.stop()

    asyncio.run(_run())

    lines = capsys.readouterr().err.splitlines()
    assert "ERROR uvicorn.error bad" in lines
    assert _ACCESS_LINE in lines


@pytest.mark.usefixtures("_restore_logging")
def test_the_lifespan_routes_uvicorn_back_after_a_later_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A uvicorn ``Config`` built with the default ``log_config`` after
    # create_app re-runs uvicorn's dictConfig, which gives its loggers their own
    # handlers again. The lifespan starts serving with them routed back.
    monkeypatch.setenv("MCD_API_LOG__FORMAT", "text")
    app = create_app()
    uvicorn.Config(FastAPI())

    with TestClient(app):
        logging.getLogger("uvicorn.error").error("bad")
        logging.getLogger("uvicorn.access").info(*_ACCESS_ARGS)

    out = capsys.readouterr()
    lines = out.err.splitlines()
    assert "ERROR uvicorn.error bad" in lines
    assert _ACCESS_LINE in lines
    assert out.out == ""


@pytest.fixture
def _restore_warnings_capture() -> Iterator[None]:
    """Start and end with ``warnings`` uncaptured.

    pytest restores ``warnings.showwarning`` after each test, while ``logging``
    still records that it is capturing; ``captureWarnings(True)`` would then do
    nothing. Turning capture off first resets that record.
    """

    logging.captureWarnings(False)
    yield
    logging.captureWarnings(False)


@pytest.mark.usefixtures("_restore_logging", "_restore_warnings_capture")
@pytest.mark.parametrize("log_format", ["json", "text"])
def test_a_warning_goes_through_the_app_handler(
    log_format: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("info", log_format)

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("careful \x1b[2J", UserWarning, stacklevel=1)

    out = capsys.readouterr()
    assert out.out == ""
    [line] = out.err.splitlines()
    if log_format == "json":
        parsed = json.loads(line)
        assert (parsed["level"], parsed["logger"]) == ("warning", "py.warnings")
        assert "UserWarning: careful \x1b[2J" in parsed["message"]
    else:
        assert line.startswith("WARNING py.warnings ")
        assert "UserWarning: careful \\x1b[2J" in line
