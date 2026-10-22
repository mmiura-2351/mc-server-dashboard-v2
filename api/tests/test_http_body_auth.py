"""Tests that a route's credential is checked before its body is read (issue #3365).

FastAPI parses a declared body -- buffers the JSON, spools the multipart form to
disk -- before it solves the route's dependencies, so the 401 of a route that
requires authentication used to come only after the whole body had been taken:
up to 513 MiB on disk for a caller with no credential at all.

These tests pin

- the mechanism, on a small app: the dependencies that read a credential are
  solved at the route's first body read, and their refusal is the answer;
- the wiring, on the real app: no route that requires authentication reads a
  byte of an unauthenticated request (the walk), and the upload routes refuse a
  caller who is not allowed on the server the same way.

The requests are raw ASGI calls, as in ``test_http_body_limit.py``: the point is
how much of the body the app *consumed*.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import pytest
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Path,
    Query,
    Request,
    Security,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
    SecurityScopes,
)
from pydantic import BaseModel

from mc_server_dashboard_api.http_body_limit import RequestBodyLimitMiddleware
from mc_server_dashboard_api.http_problem import install_problem_handlers, problem
from mc_server_dashboard_api.servers.application.files import MAX_UPLOAD_BYTES
from tests.test_http_body_limit import (
    _endless,
    _exactly,
    _exchange,
    _multipart,
    _request,
    _served,
    _sign_in,
)

# A body whose length is not declared: nothing tells the server how much is
# still to come, so a refusal reads none of it (``_endless`` fails the test if it
# is read through).
_CHUNKED = {"transfer-encoding": "chunked", "content-type": "application/json"}

_INVALID_TOKEN = (
    b'{"type":"urn:mcsd:error:invalid_token","title":"Unauthorized",'
    b'"status":401,"reason":"invalid_token"}'
)


# --- the mechanism ----------------------------------------------------------

_scheme = HTTPBearer(auto_error=False)


async def _user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_scheme)],
) -> str:
    if credentials is None or credentials.credentials not in ("member", "outsider"):
        raise problem(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})
    return credentials.credentials


async def _member(user: Annotated[str, Depends(_user)]) -> str:
    if user != "member":
        raise problem(403, "forbidden")
    return user


async def _broken(user: Annotated[str, Depends(_user)]) -> str:
    raise RuntimeError("the store behind the gate is down")


async def _malformed(user: Annotated[str, Depends(_user)]) -> str:
    raise RequestValidationError([{"type": "uuid_parsing", "loc": ("path", "id")}])


class _Echo(BaseModel):
    value: str


def _small_app() -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)
    app.add_middleware(RequestBodyLimitMiddleware)

    @app.post("/json")
    async def parsed(body: _Echo, user: Annotated[str, Depends(_member)]) -> str:
        return user

    @app.post("/upload")
    async def upload(file: UploadFile, user: Annotated[str, Depends(_member)]) -> int:
        return len(await file.read())

    @app.post("/public")
    async def public(body: _Echo) -> str:
        return body.value

    @app.post("/streamed", dependencies=[Depends(_member)])
    async def streamed(request: Request) -> int:
        return len(await request.body())

    @app.post("/broken")
    async def broken(body: _Echo, user: Annotated[str, Depends(_broken)]) -> str:
        return user

    @app.post("/malformed")
    async def malformed(body: _Echo, user: Annotated[str, Depends(_malformed)]) -> str:
        return user

    return app


def _bearer(token: str) -> dict[str, str]:
    return {**_CHUNKED, "authorization": f"Bearer {token}"}


@pytest.mark.parametrize("path", ["/json", "/upload", "/streamed"])
@pytest.mark.parametrize("headers", [_CHUNKED, _bearer("nonsense")])
async def test_an_unauthenticated_body_is_refused_unread(
    path: str, headers: dict[str, str]
) -> None:
    result = await _request(_small_app(), "POST", path, _endless(), headers=headers)

    assert result.status == 401
    assert result.body == _INVALID_TOKEN
    assert result.headers["www-authenticate"] == "Bearer"
    assert result.consumed == 0


@pytest.mark.parametrize("path", ["/json", "/upload"])
async def test_a_caller_the_route_does_not_allow_is_refused_unread(path: str) -> None:
    result = await _request(
        _small_app(), "POST", path, _endless(), headers=_bearer("outsider")
    )

    assert result.status == 403
    assert result.json()["reason"] == "forbidden"
    assert result.consumed == 0


async def test_a_small_refused_body_is_dropped_and_the_connection_kept() -> None:
    # The everyday refusal -- an expired token on a small JSON request -- is
    # answered on a connection the client can go on using.
    body = b'{"value": "x"}'

    result = await _request(
        _small_app(),
        "POST",
        "/json",
        iter([body]),
        headers={"content-type": "application/json", "content-length": str(len(body))},
    )

    assert result.status == 401
    assert result.body == _INVALID_TOKEN
    assert "connection" not in result.headers


async def test_a_client_that_closes_after_its_request_receives_the_401() -> None:
    # Over a real socket, as for the 413 (``test_http_body_limit.py``): a
    # ``Connection: close`` client writing a body the route will not take.
    size = 4 * 1024 * 1024
    async with _served(_small_app()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /json HTTP/1.1\r\nHost: t\r\nConnection: close\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {size}\r\n\r\n".encode()
        )
        for _ in range(size // 65536):
            writer.write(b"x" * 65536)
            await writer.drain()

        response = await asyncio.wait_for(reader.read(), timeout=10)
        writer.close()

    head, _, body = response.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 401 ")
    assert body == _INVALID_TOKEN


async def test_an_allowed_caller_has_the_body_read() -> None:
    result = await _request(
        _small_app(),
        "POST",
        "/json",
        iter([b'{"value": "x"}']),
        headers=_bearer("member"),
    )

    assert result.status == 200
    assert result.json() == "member"


async def test_a_route_without_a_credential_stays_public() -> None:
    result = await _request(
        _small_app(), "POST", "/public", iter([b'{"value": "x"}']), headers=_CHUNKED
    )

    assert result.status == 200
    assert result.json() == "x"


async def test_an_overridden_dependency_decides_before_the_body_too() -> None:
    # The check is the route's own dependency, solved the way the route solves
    # it, so a substitute stands in for it here as it does there.
    app = _small_app()
    app.dependency_overrides[_user] = lambda: "member"

    result = await _request(
        app, "POST", "/json", iter([b'{"value": "x"}']), headers=_CHUNKED
    )

    assert result.status == 200


async def test_a_check_that_fails_unexpectedly_reads_no_body() -> None:
    result = await _request(
        _small_app(), "POST", "/broken", _endless(), headers=_bearer("member")
    )

    assert result.status == 500
    assert result.json()["reason"] == "internal_error"
    assert result.consumed == 0


async def test_a_check_that_reports_a_malformed_request_reads_no_body() -> None:
    # The check's own 422, the one the route gave after reading the body.
    result = await _request(
        _small_app(), "POST", "/malformed", _endless(), headers=_bearer("member")
    )

    assert result.status == 422
    assert result.json()["errors"] == [{"type": "uuid_parsing", "loc": ["path", "id"]}]
    assert result.consumed == 0


async def test_a_chunked_refusal_with_a_length_beside_it_closes_the_connection() -> (
    None
):
    # The round-1 review's probe, over a real socket: ``Content-Length: 0``
    # beside the transfer encoding was believed, so the 401 neither closed the
    # connection nor bounded how long the client could go on sending chunks.
    async with _served(_small_app()) as port:
        head, body = await _exchange(
            port,
            b"POST /json HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n1\r\nx\r\n",
        )

    assert head.startswith(b"HTTP/1.1 401 ")
    assert b"\r\nconnection: close" in head.lower()
    assert body == _INVALID_TOKEN


# --- the contract with the dependencies it solves ------------------------------
#
# The check is whatever the route declares, so every shape a dependency can take
# has to end one of two ways before the body: the credential was checked, or the
# request is refused. A check that did not run is never a check that passed.


class _Contract:
    """A small app around one credential-checking dependency, counting its calls."""

    def __init__(self) -> None:
        self.app = FastAPI()
        install_problem_handlers(self.app)
        self.app.add_middleware(RequestBodyLimitMiddleware)
        self.calls = 0

    def check(self, credentials: HTTPAuthorizationCredentials | None) -> str:
        self.calls += 1
        if credentials is None or credentials.credentials != "member":
            raise problem(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})
        return credentials.credentials


_Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(_scheme)]


@pytest.mark.parametrize("token", [None, "member"])
async def test_a_check_with_a_malformed_path_parameter_reads_no_body(
    token: str | None,
) -> None:
    contract = _Contract()

    async def auth(credentials: _Credentials, item: Annotated[int, Path()]) -> str:
        return contract.check(credentials)

    @contract.app.post("/items/{item}")
    async def route(body: _Echo, user: str = Depends(auth)) -> str:
        return user

    headers = _CHUNKED if token is None else _bearer(token)
    refused = await _request(
        contract.app, "POST", "/items/nope", _endless(), headers=headers
    )

    # The check could not be made, so the body is not read: the request is
    # answered with the validation error that kept it from being made.
    assert refused.status == 422
    assert [e["loc"] for e in refused.json()["errors"]] == [["path", "item"]]
    assert refused.consumed == 0

    unauthenticated = await _request(
        contract.app, "POST", "/items/1", _endless(), headers=_CHUNKED
    )
    assert (unauthenticated.status, unauthenticated.consumed) == (401, 0)


async def test_a_check_with_a_malformed_query_parameter_reads_no_body() -> None:
    contract = _Contract()

    async def auth(credentials: _Credentials, page: Annotated[int, Query()]) -> str:
        return contract.check(credentials)

    @contract.app.post("/items")
    async def route(body: _Echo, user: str = Depends(auth)) -> str:
        return user

    result = await _request(
        contract.app, "POST", "/items", _endless(), headers=_CHUNKED
    )

    assert result.status == 422
    assert [e["loc"] for e in result.json()["errors"]] == [["query", "page"]]
    assert (result.consumed, contract.calls) == (0, 0)


async def test_a_check_that_wants_the_body_is_refused_rather_than_fed_it() -> None:
    # A credential check cannot depend on the body it stands in front of. Such a
    # route answers 422 to everyone, allowed or not, which no test of it survives.
    contract = _Contract()

    async def auth(credentials: _Credentials, payload: _Echo) -> str:
        return contract.check(credentials)

    @contract.app.post("/items")
    async def route(user: str = Depends(auth)) -> str:
        return user

    result = await _request(
        contract.app, "POST", "/items", _endless(), headers=_bearer("member")
    )

    assert result.status == 422
    assert (result.consumed, contract.calls) == (0, 0)


async def test_a_check_with_security_scopes_is_given_them() -> None:
    contract = _Contract()
    seen: list[list[str]] = []

    async def auth(scopes: SecurityScopes, credentials: _Credentials) -> str:
        seen.append(scopes.scopes)
        return contract.check(credentials)

    @contract.app.post("/items")
    async def route(
        body: _Echo, user: str = Security(auth, scopes=["items:write"])
    ) -> str:
        return user

    refused = await _request(
        contract.app, "POST", "/items", _endless(), headers=_CHUNKED
    )
    assert (refused.status, refused.consumed) == (401, 0)
    assert seen == [["items:write"]]

    allowed = await _request(
        contract.app,
        "POST",
        "/items",
        iter([b'{"value": "x"}']),
        headers=_bearer("member"),
    )
    assert allowed.status == 200
    # Before the body, and again by the handler: the same scopes each time.
    assert seen == [["items:write"]] * 3


@pytest.mark.parametrize(("token", "resolutions"), [(None, 1), ("member", 2)])
async def test_a_check_that_yields_is_cleaned_up_once_per_resolution(
    token: str | None, resolutions: int
) -> None:
    contract = _Contract()
    opened = closed = 0

    async def session() -> AsyncIterator[None]:
        nonlocal opened, closed
        opened += 1
        try:
            yield
        finally:
            closed += 1

    async def auth(credentials: _Credentials, _: None = Depends(session)) -> str:
        return contract.check(credentials)

    @contract.app.post("/items")
    async def route(body: _Echo, user: str = Depends(auth)) -> str:
        return user

    headers = _CHUNKED if token is None else _bearer(token)
    await _request(
        contract.app, "POST", "/items", iter([b'{"value": "x"}']), headers=headers
    )

    # Refused before the body: resolved once. Allowed: once more by the handler.
    assert (opened, closed) == (resolutions, resolutions)


async def test_a_check_that_is_a_class_reads_no_body() -> None:
    contract = _Contract()

    class Gate:
        def __init__(self, credentials: _Credentials) -> None:
            self.user = contract.check(credentials)

    @contract.app.post("/items")
    async def route(body: _Echo, gate: Any = Depends(Gate)) -> str:
        return str(gate.user)

    result = await _request(
        contract.app, "POST", "/items", _endless(), headers=_CHUNKED
    )

    assert (result.status, result.consumed, contract.calls) == (401, 0, 1)


async def test_a_check_declared_on_the_router_reads_no_body() -> None:
    contract = _Contract()

    async def auth(credentials: _Credentials) -> str:
        return contract.check(credentials)

    router = APIRouter(dependencies=[Depends(auth)])

    @router.post("/items")
    async def route(body: _Echo) -> str:
        return body.value

    contract.app.include_router(router)

    result = await _request(
        contract.app, "POST", "/items", _endless(), headers=_CHUNKED
    )

    assert (result.status, result.consumed, contract.calls) == (401, 0, 1)


# --- the wiring: every route that requires authentication ----------------------

_BODYLESS_METHODS = {"GET", "HEAD", "OPTIONS"}

# The routes that take a request from a caller with no credential. Everything
# else must answer such a caller 401 without reading its body.
_PUBLIC = {
    ("POST", "/api/users"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/session"),
    ("POST", "/api/auth/logout"),
}


@pytest.fixture
async def live_app(shared_app: FastAPI) -> AsyncIterator[FastAPI]:
    """The real app with its lifespan open: its dependencies read ``app.state``."""

    async with shared_app.router.lifespan_context(shared_app):
        yield shared_app


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)


def _malformed_ids(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "not-an-id", path)


def _body_routes(app: FastAPI) -> Iterator[tuple[str, str]]:
    for route in app.routes:
        if isinstance(route, APIRoute):
            assert route.methods is not None
            for method in sorted(route.methods - _BODYLESS_METHODS):
                yield method, route.path


async def test_no_route_reads_a_body_before_authentication(live_app: FastAPI) -> None:
    routes = set(_body_routes(live_app))
    assert routes >= _PUBLIC

    for method, path in sorted(routes - _PUBLIC):
        # With well-formed ids and with malformed ones: a route whose check
        # validated its path before the credential would answer the latter 422.
        for concrete in (_concrete(path), _malformed_ids(path)):
            result = await _request(
                live_app, method, concrete, _endless(), headers=_CHUNKED
            )

            assert (result.status, result.consumed) == (401, 0), (method, concrete)


_API = "/api/communities/{community_id}/servers"
_UPLOADS = [
    f"{_API}/{{server_id}}/files/upload",
    f"{_API}/import",
    f"{_API}/{{server_id}}/backups/upload",
    f"{_API}/{{server_id}}/plugins",
    "/api/resource-packs",
]


def _upload(size: int) -> tuple[dict[str, str], Iterator[bytes]]:
    """A multipart upload declaring ``size`` bytes, whose body is never to be read."""

    headers, _ = _multipart(0)
    return {**headers, "content-length": str(size)}, _endless()


@pytest.mark.parametrize("path", _UPLOADS)
@pytest.mark.parametrize("credential", [None, "Bearer nonsense"])
async def test_an_upload_without_a_valid_credential_is_refused_unread(
    live_app: FastAPI, path: str, credential: str | None
) -> None:
    # A body within the route's limit, so only the credential refuses it: this is
    # the request that used to be spooled to disk in full before its 401.
    headers, chunks = _upload(MAX_UPLOAD_BYTES // 2)
    if credential is not None:
        headers["authorization"] = credential

    result = await _request(live_app, "POST", _concrete(path), chunks, headers=headers)

    assert result.status == 401
    assert result.body == _INVALID_TOKEN
    assert result.headers["www-authenticate"] == "Bearer"
    assert result.consumed == 0
    # Nor is the upload read and dropped afterwards: the connection closes.
    assert result.headers["connection"] == "close"


@pytest.mark.parametrize("credential", [None, "Bearer nonsense"])
async def test_a_json_write_without_a_valid_credential_is_refused_unread(
    live_app: FastAPI, credential: str | None
) -> None:
    headers = dict(_CHUNKED)
    if credential is not None:
        headers["authorization"] = credential

    result = await _request(
        live_app,
        "PUT",
        _concrete(f"{_API}/{{server_id}}/files"),
        _endless(),
        headers=headers,
    )

    assert result.status == 401
    assert result.body == _INVALID_TOKEN
    assert result.consumed == 0


@pytest.mark.parametrize("path", [p for p in _UPLOADS if p.startswith(_API)])
@pytest.mark.parametrize(("member", "status"), [(False, 404), (True, 403)])
async def test_an_upload_the_caller_may_not_make_is_refused_unread(
    live_app: FastAPI, path: str, member: bool, status: int
) -> None:
    # Authenticated, but not a member of the community (404, no existence signal)
    # or without the permission on this server (403): the same two-layer answer
    # as before, now given before the upload is taken.
    _sign_in(live_app, member=member, allow=False)
    headers, chunks = _upload(MAX_UPLOAD_BYTES // 2)

    result = await _request(live_app, "POST", _concrete(path), chunks, headers=headers)

    assert result.status == status
    assert result.consumed == 0


async def test_a_caller_the_route_allows_has_the_body_read(
    live_app: FastAPI,
) -> None:
    _sign_in(live_app)

    result = await _request(
        live_app,
        "PATCH",
        _concrete("/api/communities/{community_id}"),
        _exactly(1024),
        headers=_CHUNKED,
    )

    # Past the gate the body is read, and found not to be the JSON it should be.
    assert result.status == 422
    assert result.consumed == 1024


# --- every method, and no route at all -----------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        # An authenticated route, a public one, and a path nothing matches.
        ("GET", "/api/users/me", 401),
        ("GET", "/api/healthz", 200),
        ("OPTIONS", "/api/users/me", 405),
        ("GET", "/api/no-such-route", 404),
        ("POST", "/api/no-such-route", 404),
        ("DELETE", "/api/no-such-route", 404),
    ],
)
@pytest.mark.parametrize(
    "framing",
    [{"content-length": str(64 * 1024 * 1024)}, {"transfer-encoding": "chunked"}],
)
async def test_an_unread_body_on_any_request_closes_the_connection(
    live_app: FastAPI, method: str, path: str, status: int, framing: dict[str, str]
) -> None:
    result = await _request(live_app, method, path, _endless(), headers=framing)

    assert result.status == status
    assert result.consumed == 0
    assert result.headers["connection"] == "close"


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/api/users/me", 401),
        ("GET", "/api/healthz", 200),
        ("GET", "/api/no-such-route", 404),
    ],
)
async def test_a_request_without_a_body_is_not_touched(
    live_app: FastAPI, method: str, path: str, status: int
) -> None:
    result = await _request(live_app, method, path, iter([]))

    assert result.status == status
    assert "connection" not in result.headers
