"""A database outage answers 503 ``database_unavailable`` on every REST route (#4010).

The problem handler maps it once, for every route: an engine that refuses the
connect, a server that is full, starting up or shutting down, and a connection
lost mid-query are "retry later", answered 503 with ``Retry-After`` and logged
as one rate-limited WARNING line, the traceback at DEBUG. Any other database
error, a protocol violation included, stays a 500 with its traceback.

The route is ``GET /api/users/me``: authenticating its caller is the first thing
that reads the database, as it is for most routes.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Iterator

import asyncpg
import pytest
import sqlalchemy.exc
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api import http_problem
from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import get_authenticate_request
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
)
from tests.client_utils import enter_client
from tests.core.fakes import server_error
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
    monkeypatch.setattr(http_problem, "_last_outage_warning", None, raising=False)


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
    ],
)
def test_another_database_error_stays_a_500_with_its_traceback(
    client: TestClient, logs: pytest.LogCaptureFixture, error: Exception
) -> None:
    assert _get_me(client, error) == (500, _INTERNAL, None)

    [logged] = _records(logs, logging.ERROR)
    assert logged.exc_info is not None
    assert _records(logs, logging.WARNING) == []


def test_an_outage_warns_at_most_every_30_seconds(
    client: TestClient, logs: pytest.LogCaptureFixture
) -> None:
    full = server_error(asyncpg.TooManyConnectionsError("too many clients"))

    for _ in range(3):
        assert _get_me(client, full)[0] == 503
    assert len(_records(logs, logging.WARNING)) == 1

    # 29 s after the WARNING the outage is still the same one.
    http_problem._last_outage_warning = time.monotonic() - 29
    assert _get_me(client, full)[0] == 503
    assert len(_records(logs, logging.WARNING)) == 1

    http_problem._last_outage_warning = time.monotonic() - 30
    assert _get_me(client, full)[0] == 503
    assert len(_records(logs, logging.WARNING)) == 2
    # Every one of them left its traceback at DEBUG.
    assert len(_records(logs, logging.DEBUG)) == 5


def test_a_refused_connect_answers_503(
    monkeypatch: pytest.MonkeyPatch, logs: pytest.LogCaptureFixture
) -> None:
    # The app's own engine, against a port nothing listens on: what the driver
    # really raises, not a hand-built error.
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@127.0.0.1:5/none"
    )
    app = create_app()
    client = enter_client(TestClient(app, raise_server_exceptions=False))
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
