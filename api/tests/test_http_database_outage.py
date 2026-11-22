"""A database outage answers 503 ``database_unavailable`` on every REST route (#4010).

The problem handler maps it once, for every route: an engine that refuses the
connect, a server that is full, starting up or shutting down, and a connection
lost mid-query are "retry later", answered 503 with ``Retry-After`` and logged
as one WARNING line per cause, rate-limited, the traceback at DEBUG. Any other
database error, a protocol violation or a driver error on a live connection
included, stays a 500 with its traceback.

The route is ``GET /api/users/me``: authenticating its caller is the first thing
that reads the database, as it is for most routes.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from types import SimpleNamespace

import asyncpg
import pytest
import sqlalchemy.exc
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi

from mc_server_dashboard_api import http_problem
from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.core.adapters.database import (
    DatabaseConnectError,
    DatabaseTimeoutError,
    is_transient_database_error,
)
from mc_server_dashboard_api.dependencies import get_authenticate_request
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
)
from tests.client_utils import enter_client
from tests.core.fakes import (
    black_hole_database,
    first_failure,
    interface_error,
    server_error,
    silent_database,
)
from tests.identity.fakes import (
    FakeClock,
    FakeTokenService,
    FakeUnitOfWork,
    make_user,
)

_ROUTE = "/api/users/me"

_UNAVAILABLE = {
    "type": "urn:mcsd:error:database_unavailable",
    "title": "Service Unavailable",
    "status": 503,
    "reason": "database_unavailable",
}

_INTERNAL = {
    "type": "urn:mcsd:error:internal_error",
    "title": "Internal Server Error",
    "status": 500,
    "reason": "internal_error",
}


@pytest.fixture(autouse=True)
def _no_recent_outage_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    # The limiter is process-wide, so another test's outage would hold this
    # one's WARNING back.
    monkeypatch.setattr(http_problem, "_outage_warnings", {})


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The handler's own clock, so a slow request cannot move a boundary."""

    now = [1000.0]
    monkeypatch.setattr(http_problem, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG, logger=http_problem.__name__)
    return caplog


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == http_problem.__name__ and r.levelno == level
    ]


class _FailingUnitOfWork(FakeUnitOfWork):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def __aenter__(self) -> FakeUnitOfWork:
        raise self._error


@pytest.fixture
def client(shared_app: FastAPI) -> Iterator[TestClient]:
    yield enter_client(TestClient(shared_app, raise_server_exceptions=False))


def _get_me(client: TestClient, error: Exception) -> tuple[int, object, str | None]:
    """``GET /api/users/me`` whose user lookup meets ``error``."""

    tokens = FakeTokenService()
    use_case = AuthenticateRequest(uow=_FailingUnitOfWork(error), tokens=tokens)
    app = client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_authenticate_request] = lambda: use_case
    token = tokens.issue_access_token(make_user().id)
    resp = client.get(_ROUTE, headers={"Authorization": f"Bearer {token}"})
    assert resp.headers["content-type"] == "application/problem+json"
    return resp.status_code, resp.json(), resp.headers.get("retry-after")


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            server_error(
                asyncpg.TooManyConnectionsError("sorry, too many clients already")
            ),
            id="53300-full",
        ),
        pytest.param(
            server_error(
                asyncpg.CannotConnectNowError("the database system is starting up")
            ),
            id="57P03-starting-up",
        ),
        pytest.param(
            server_error(
                asyncpg.ConnectionDoesNotExistError(
                    "connection was closed in the middle of operation"
                ),
                invalidated=True,
            ),
            id="08003-invalidated",
        ),
        # A connection the driver found closed: lost, as SQLAlchemy says.
        pytest.param(
            interface_error(
                asyncpg.InterfaceError("connection is closed"), invalidated=True
            ),
            id="interface-invalidated",
        ),
        # A statement that outlasted the engine's bound (issue #4042).
        pytest.param(
            DatabaseTimeoutError("the database did not answer"),
            id="statement-timeout",
        ),
    ],
)
def test_an_outage_answers_503_with_one_warning(
    client: TestClient, logs: pytest.LogCaptureFixture, error: Exception
) -> None:
    assert _get_me(client, error) == (503, _UNAVAILABLE, "5")

    [warning] = _records(logs, logging.WARNING)
    assert warning.exc_info is None
    assert _ROUTE in warning.getMessage()
    assert [r.exc_info is not None for r in _records(logs, logging.DEBUG)] == [True]
    assert _records(logs, logging.ERROR) == []


@pytest.mark.parametrize(
    "error",
    [
        # A protocol violation points at the driver or a proxy, not an outage,
        # even when the server closed the connection over it.
        pytest.param(
            server_error(asyncpg.ProtocolViolationError("invalid message format")),
            id="08P01",
        ),
        pytest.param(
            server_error(
                asyncpg.ProtocolViolationError("invalid message format"),
                invalidated=True,
            ),
            id="08P01-invalidated",
        ),
        # Wrong credentials or database, and a programming error: retrying does
        # not fix them.
        pytest.param(
            server_error(
                asyncpg.InvalidPasswordError("password authentication failed")
            ),
            id="28P01",
        ),
        pytest.param(
            server_error(asyncpg.InvalidCatalogNameError("database does not exist")),
            id="3D000",
        ),
        pytest.param(
            sqlalchemy.exc.ProgrammingError(
                "SELECT 1", None, Exception("syntax error")
            ),
            id="programming",
        ),
        # Two operations at once on one connection: a concurrency bug, on a
        # connection that is fine.
        pytest.param(
            interface_error(
                asyncpg.InterfaceError(
                    "cannot perform operation: another operation is in progress"
                )
            ),
            id="interface-live-connection",
        ),
    ],
)
def test_another_database_error_stays_a_500_with_its_traceback(
    client: TestClient, logs: pytest.LogCaptureFixture, error: Exception
) -> None:
    assert _get_me(client, error) == (500, _INTERNAL, None)

    [logged] = _records(logs, logging.ERROR)
    assert logged.exc_info is not None
    assert _records(logs, logging.WARNING) == []


def test_a_timeout_that_is_not_the_databases_stays_a_500(
    client: TestClient, logs: pytest.LogCaptureFixture
) -> None:
    # A store's or a Worker's bare ``TimeoutError`` is not taken for a database
    # outage: the engine's own arrives typed (issue #4042).
    assert _get_me(client, TimeoutError("the store did not answer")) == (
        500,
        _INTERNAL,
        None,
    )

    [logged] = _records(logs, logging.ERROR)
    assert logged.exc_info is not None


def test_an_outage_warns_at_most_every_30_seconds(
    client: TestClient, logs: pytest.LogCaptureFixture, clock: list[float]
) -> None:
    full = server_error(asyncpg.TooManyConnectionsError("too many clients"))

    for _ in range(3):
        assert _get_me(client, full)[0] == 503
    assert len(_records(logs, logging.WARNING)) == 1

    # Just short of 30 s after the WARNING the outage is still the same one.
    clock[0] = 1029.999
    assert _get_me(client, full)[0] == 503
    assert len(_records(logs, logging.WARNING)) == 1

    clock[0] = 1030.0
    assert _get_me(client, full)[0] == 503
    assert len(_records(logs, logging.WARNING)) == 2
    # Every one of them left its traceback at DEBUG.
    assert len(_records(logs, logging.DEBUG)) == 5


def test_a_new_cause_inside_the_window_is_named(
    client: TestClient, logs: pytest.LogCaptureFixture, clock: list[float]
) -> None:
    full = server_error(asyncpg.TooManyConnectionsError("too many clients"))
    starting = server_error(asyncpg.CannotConnectNowError("starting up"))

    for error in (full, starting, full, starting):
        assert _get_me(client, error)[0] == 503
        clock[0] += 1

    warnings = [r.getMessage() for r in _records(logs, logging.WARNING)]
    assert len(warnings) == 2
    assert "too many clients" in warnings[0]
    assert "starting up" in warnings[1]


def test_the_warning_names_the_cause_and_not_the_statement(
    client: TestClient, logs: pytest.LogCaptureFixture
) -> None:
    # SQLAlchemy renders the statement and its parameters on the lines after
    # the cause; they can carry a caller's data.
    error = asyncpg.TooManyConnectionsError("sorry, too many clients already")
    orig = AsyncAdapt_asyncpg_dbapi.Error(error.args[0], error)  # type: ignore[no-untyped-call]
    wrapped = sqlalchemy.exc.DBAPIError.instance(
        "SELECT * FROM app_user WHERE email = $1",
        ("someone@example.com",),
        orig,
        AsyncAdapt_asyncpg_dbapi.Error,
    )
    assert "[SQL:" in str(wrapped)

    assert _get_me(client, wrapped)[0] == 503

    [warning] = _records(logs, logging.WARNING)
    message = warning.getMessage()
    assert "DBAPIError" in message
    assert "sorry, too many clients already" in message
    assert "[SQL:" not in message
    assert "app_user" not in message
    assert "someone@example.com" not in message


def test_the_causes_remembered_are_bounded(
    client: TestClient, logs: pytest.LogCaptureFixture, clock: list[float]
) -> None:
    # Each cause is named once inside its window, however many there are; only
    # the latest 16 are kept.
    for i in range(20):
        error = server_error(asyncpg.TooManyConnectionsError(f"too many {i}"))
        assert _get_me(client, error)[0] == 503

    assert len(_records(logs, logging.WARNING)) == 20
    assert len(http_problem._outage_warnings) == 16


def _refused_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real app, its lifespan open, its database a port nothing listens on."""

    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@127.0.0.1:5/none"
    )
    return enter_client(TestClient(create_app(), raise_server_exceptions=False))


def test_a_refused_connect_answers_503(
    monkeypatch: pytest.MonkeyPatch, logs: pytest.LogCaptureFixture
) -> None:
    # The app's own engine: what the driver really raises, not a hand-built
    # error.
    client = _refused_client(monkeypatch)
    app = client.app
    assert isinstance(app, FastAPI)
    key = app.state.settings.auth.token.signing_key
    assert isinstance(key, str)
    tokens = JwtTokenService(
        signing_key=key,
        algorithm="HS256",
        access_ttl=dt.timedelta(minutes=15),
        download_grant_ttl=dt.timedelta(minutes=1),
        download_cookie_ttl=dt.timedelta(minutes=1),
        clock=FakeClock(dt.datetime.now(dt.UTC)),
    )
    token = tokens.issue_access_token(make_user().id)

    resp = client.get(_ROUTE, headers={"Authorization": f"Bearer {token}"})

    assert (resp.status_code, resp.json()) == (503, _UNAVAILABLE)
    assert resp.headers["retry-after"] == "5"
    [warning] = _records(logs, logging.WARNING)
    assert warning.exc_info is None
    assert _records(logs, logging.ERROR) == []


@pytest.mark.parametrize("engine", ["engine", "assignment_fence_engine"])
def test_each_app_engine_raises_a_refused_connect_typed(
    monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    # Typed, the refused connect is told apart from a disk fault's bare
    # ``OSError``, which stays a 500.
    client = _refused_client(monkeypatch)
    app = client.app
    assert isinstance(app, FastAPI) and client.portal is not None

    async def connect() -> None:
        async with getattr(app.state, engine).connect():
            pass

    with pytest.raises(DatabaseConnectError):
        client.portal.call(connect)


# The bound the stalled-server tests configure, and how long they allow for it:
# generous against a loaded host, far below what the driver applies unbounded.
_BOUND = "0.2"
_WITHIN_SECONDS = 2.0


def _bounded_client(
    monkeypatch: pytest.MonkeyPatch, url: str, *, setting: str
) -> TestClient:
    """The real app, its lifespan open, its database ``url``, ``setting`` small."""

    monkeypatch.setenv("MCD_API_DATABASE__URL", url)
    monkeypatch.setenv(f"MCD_API_DATABASE__{setting.upper()}", _BOUND)
    return enter_client(TestClient(create_app(), raise_server_exceptions=False))


def _first_query_failure(client: TestClient, engine: str) -> tuple[Exception, float]:
    app = client.app
    assert isinstance(app, FastAPI) and client.portal is not None

    async def query() -> None:
        async with getattr(app.state, engine).connect() as conn:
            await conn.execute(text("SELECT 1"))

    return client.portal.call(first_failure, query)


@pytest.mark.parametrize("engine", ["engine", "assignment_fence_engine"])
def test_each_app_engine_bounds_a_connect_the_server_never_answers(
    monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    # A database host that stopped responding fails a connect at the configured
    # bound (issue #4042), typed as the refused connect is.
    with black_hole_database() as url:
        client = _bounded_client(monkeypatch, url, setting="connect_timeout_seconds")
        error, elapsed = _first_query_failure(client, engine)

    assert elapsed < _WITHIN_SECONDS
    assert isinstance(error, DatabaseConnectError)


# A stall guard: without the bound the call never returns.
@pytest.mark.timeout(10)
def test_a_statement_the_database_never_answers_answers_503(
    monkeypatch: pytest.MonkeyPatch, logs: pytest.LogCaptureFixture
) -> None:
    # The app's own engine at its configured bound: what the driver really
    # raises, typed on its way out (issue #4042).
    with silent_database() as url:
        client = _bounded_client(monkeypatch, url, setting="statement_timeout_seconds")
        app = client.app
        assert isinstance(app, FastAPI)
        key = app.state.settings.auth.token.signing_key
        assert isinstance(key, str)
        tokens = JwtTokenService(
            signing_key=key,
            algorithm="HS256",
            access_ttl=dt.timedelta(minutes=15),
            download_grant_ttl=dt.timedelta(minutes=1),
            download_cookie_ttl=dt.timedelta(minutes=1),
            clock=FakeClock(dt.datetime.now(dt.UTC)),
        )
        token = tokens.issue_access_token(make_user().id)

        resp = client.get(_ROUTE, headers={"Authorization": f"Bearer {token}"})

    assert (resp.status_code, resp.json()) == (503, _UNAVAILABLE)
    assert resp.headers["retry-after"] == "5"
    assert _records(logs, logging.ERROR) == []


@pytest.mark.parametrize("engine", ["engine", "assignment_fence_engine"])
def test_each_app_engine_bounds_a_query_the_server_never_answers(
    monkeypatch: pytest.MonkeyPatch, engine: str
) -> None:
    # A connection gone silent fails its statement at the configured bound
    # (issue #4042), as an outage.
    with silent_database() as url:
        client = _bounded_client(monkeypatch, url, setting="statement_timeout_seconds")
        error, elapsed = _first_query_failure(client, engine)

    assert elapsed < _WITHIN_SECONDS
    assert is_transient_database_error(error) is True
