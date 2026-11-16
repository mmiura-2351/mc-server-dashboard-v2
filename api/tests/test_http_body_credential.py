"""Tests that a request without a valid credential cannot make the API read more
than the anonymous body limit (issues #3365, #3561).

FastAPI parses a declared body -- buffers the JSON, spools the multipart form to
disk -- before it solves the route's dependencies, the authentication among
them. So the body-limit middleware judges, at a request's first body read and
from its headers alone, whether the request carries a valid credential: one that
does keeps its route's limit, one that does not is held to
``ANONYMOUS_MAX_REQUEST_BODY_BYTES``. These tests pin

- the mechanism, on a small app with a stand-in check: the anonymous limit, by
  declared length before a byte is read and by running count; the route's own
  answer, byte for byte, to an anonymous body within it; the route's limit for a
  credentialled body; the route's 403 after the read; every shape a route's
  check can take;
- the real check (:func:`~mc_server_dashboard_api.dependencies.has_body_credential`)
  on the real app: what it counts as a credential, what it does when the database
  is down, and that ``get_current_user`` takes the user it verified;
- the wiring, on the real app: no route reads more than the anonymous limit of a
  request without a credential (the walk), and every route that takes a body
  from such a caller fits within it at its largest legitimate body (the
  inventory).

The requests are raw ASGI calls, as in ``test_http_body_limit.py``: the point is
how much of the body the app *consumed* -- counted at the ASGI ``receive``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
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
    Request,
    Security,
    UploadFile,
)
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
    SecurityScopes,
)
from pydantic import BaseModel

from mc_server_dashboard_api import dependencies
from mc_server_dashboard_api.dependencies import (
    get_authenticate_request,
    get_login,
    get_logout,
    get_refresh_session,
    get_register_user,
    get_restore_session,
    get_update_profile,
)
from mc_server_dashboard_api.http_body_limit import (
    ANONYMOUS_MAX_REQUEST_BODY_BYTES,
    ANONYMOUS_REASON,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    EARLY_ANSWER_DISCARD_BYTES,
    RequestBodyLimitMiddleware,
    request_body_limit,
)
from mc_server_dashboard_api.http_problem import (
    install_problem_handlers,
    problem,
    unhandled_exception_middleware,
)
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
    Authentication,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.token_service import VerifiedAccessToken
from mc_server_dashboard_api.servers.api.files import MAX_WRITE_BODY_BYTES
from mc_server_dashboard_api.servers.application.files import MAX_UPLOAD_BYTES
from tests.identity.fakes import (
    FakeClock,
    FakeTokenService,
    FakeUnitOfWork,
    make_user,
)
from tests.test_http_body_limit import (
    _endless,
    _exactly,
    _exchange,
    _multipart,
    _problem,
    _request,
    _served,
    _sign_in,
)

_ANONYMOUS = ANONYMOUS_MAX_REQUEST_BODY_BYTES

# Small chunks, so a count that crosses the anonymous limit is seen within one
# kibibyte of it: "no more than the limit" is then a tight bound.
_TRICKLE = 1024


def _trickle() -> Iterator[bytes]:
    """An endless body in small chunks; fails the test if it is read to 64 MiB."""

    for _ in range(64 * 1024 * 1024 // _TRICKLE):
        yield b"x" * _TRICKLE
    raise AssertionError("the app read 64 MiB of a body it should have refused")


# A body whose length is not declared: only the running count bounds it.
_CHUNKED = {"transfer-encoding": "chunked", "content-type": "application/json"}

_INVALID_TOKEN = (
    b'{"type":"urn:mcsd:error:invalid_token","title":"Unauthorized",'
    b'"status":401,"reason":"invalid_token"}'
)

_CREDENTIAL_REQUIRED = _problem(ANONYMOUS_REASON)


def _json_of(size: int) -> bytes:
    """``{"value": "x"}`` padded with insignificant whitespace to ``size`` bytes."""

    body = b'{"value": "x"}'
    return body + b" " * (size - len(body))


def _declared(size: int, **headers: str) -> dict[str, str]:
    return {"content-type": "application/json", "content-length": str(size), **headers}


# --- the mechanism ----------------------------------------------------------

_scheme = HTTPBearer(auto_error=False)
_VALID = ("member", "outsider")


async def _credentialled(request: Request) -> bool:
    # The stand-in for the app's check: a valid token is one of _VALID.
    credentials = await _scheme(request)
    return credentials is not None and credentials.credentials in _VALID


async def _user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_scheme)],
) -> str:
    if credentials is None or credentials.credentials not in _VALID:
        raise problem(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})
    return credentials.credentials


async def _member(user: Annotated[str, Depends(_user)]) -> str:
    if user != "member":
        raise problem(403, "forbidden")
    return user


async def _broken(user: Annotated[str, Depends(_user)]) -> str:
    raise RuntimeError("the store behind the check is down")


class _Echo(BaseModel):
    value: str


_UPLOAD_LIMIT = 3 * DEFAULT_MAX_REQUEST_BODY_BYTES


def _small_app(*, limited: bool = True) -> FastAPI:
    """The routes under test; ``limited=False`` builds them without the middleware.

    The unlimited app is the reference: within the anonymous limit, the limited
    app answers exactly as it does.
    """

    app = FastAPI()
    install_problem_handlers(app)
    if limited:
        app.add_middleware(RequestBodyLimitMiddleware, credentialled=_credentialled)
    # As in the real app: an unexpected failure is answered 500, not raised.
    app.middleware("http")(unhandled_exception_middleware)

    @app.post("/json")
    async def parsed(body: _Echo, user: Annotated[str, Depends(_member)]) -> str:
        return user

    @app.post("/upload")
    @request_body_limit(_UPLOAD_LIMIT)
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

    @app.post("/items/{item}")
    async def item(
        body: _Echo,
        item: Annotated[int, Path()],
        user: Annotated[str, Depends(_member)],
    ) -> str:
        return user

    return app


def _bearer(token: str, **headers: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", **headers}


# A request without a valid credential: none at all, an invalid token, and an
# Authorization header that is not a Bearer token at all.
_ANONYMOUS_CREDENTIALS: list[dict[str, str]] = [
    {},
    {"authorization": "Bearer nonsense"},
    {"authorization": "Basic bWVtYmVyOg=="},
]


def _content_type(path: str) -> dict[str, str]:
    if path == "/upload":
        return _multipart(0)[0]
    return {"content-type": "application/json"}


@pytest.mark.parametrize("path", ["/json", "/upload", "/public"])
@pytest.mark.parametrize("credential", _ANONYMOUS_CREDENTIALS)
async def test_an_anonymous_body_declared_past_the_limit_is_refused_unread(
    path: str, credential: dict[str, str]
) -> None:
    # A length past what the server would read and drop after its answer, so
    # every byte consumed would have been read for the route.
    size = EARLY_ANSWER_DISCARD_BYTES + 1

    result = await _request(
        _small_app(),
        "POST",
        path,
        _endless(),
        headers={**_content_type(path), "content-length": str(size), **credential},
    )

    assert result.status == 413
    assert result.json() == _CREDENTIAL_REQUIRED
    assert result.consumed == 0
    assert result.headers["connection"] == "close"


@pytest.mark.parametrize("path", ["/json", "/upload", "/public"])
async def test_an_anonymous_body_declared_one_byte_past_the_limit_is_refused(
    path: str,
) -> None:
    # Refused by what it declares: the 14 bytes it carries would have been read
    # and answered by the route.
    result = await _request(
        _small_app(),
        "POST",
        path,
        iter([b'{"value": "x"}']),
        headers={**_content_type(path), "content-length": str(_ANONYMOUS + 1)},
    )

    assert result.status == 413
    assert result.json() == _CREDENTIAL_REQUIRED


@pytest.mark.parametrize("path", ["/json", "/upload", "/public"])
@pytest.mark.parametrize("credential", _ANONYMOUS_CREDENTIALS)
@pytest.mark.parametrize(
    "framing", [{"transfer-encoding": "chunked"}, {"content-length": "10"}]
)
async def test_an_anonymous_body_streamed_past_the_limit_is_cut_off_at_it(
    path: str, credential: dict[str, str], framing: dict[str, str]
) -> None:
    # Chunked, or under-declared: the running count stops the read as soon as it
    # crosses the limit.
    result = await _request(
        _small_app(),
        "POST",
        path,
        _streamed(path),
        headers={**_content_type(path), **framing, **credential},
    )

    assert result.status == 413
    assert result.json() == _CREDENTIAL_REQUIRED
    assert _ANONYMOUS < result.consumed <= _ANONYMOUS + _TRICKLE


async def test_a_route_that_checks_its_caller_before_it_reads_answers_first() -> None:
    # A route that reads its body itself runs its dependencies first, as always.
    result = await _request(
        _small_app(), "POST", "/streamed", _endless(), headers=_CHUNKED
    )

    assert result.status == 401
    assert result.body == _INVALID_TOKEN
    assert result.consumed == 0


def _streamed(path: str) -> Iterator[bytes]:
    """An endless body for ``path``: an upload's starts as a file part."""

    if path == "/upload":
        yield next(_multipart(0)[1])
    yield from _trickle()


def _upload_of(total: int) -> tuple[dict[str, str], Iterator[bytes]]:
    """A multipart upload whose whole body is ``total`` bytes."""

    headers, chunks = _multipart(0)
    envelope = sum(len(chunk) for chunk in chunks)
    return _multipart(total - envelope)


def _body(path: str, size: int) -> tuple[dict[str, str], Iterator[bytes]]:
    if path == "/upload":
        return _upload_of(size)
    return {"content-type": "application/json"}, iter([_json_of(size)])


@pytest.mark.parametrize("path", ["/json", "/upload", "/items/nope", "/items/1"])
@pytest.mark.parametrize("credential", _ANONYMOUS_CREDENTIALS)
@pytest.mark.parametrize("size", [1024, _ANONYMOUS])
async def test_an_anonymous_body_within_the_limit_gets_the_routes_own_answer(
    path: str, credential: dict[str, str], size: int
) -> None:
    # Read whole, and answered exactly as without the limit: the 401, or the 422
    # of a malformed path, is the route's own.
    answers = []
    for limited in (True, False):
        headers, chunks = _body(path, size)
        result = await _request(
            _small_app(limited=limited),
            "POST",
            path,
            chunks,
            headers={**headers, **credential, "content-length": str(size)},
        )
        answers.append((result.status, result.headers, result.body))
        assert result.consumed == size

    assert answers[0] == answers[1]
    if path != "/items/nope":
        status, headers, body = answers[0]
        assert (status, body) == (401, _INVALID_TOKEN)
        assert headers["www-authenticate"] == "Bearer"
        # Read through, so nothing is left to drop: the connection stays usable.
        assert "connection" not in headers


@pytest.mark.parametrize(
    ("path", "size", "read"),
    [
        ("/json", DEFAULT_MAX_REQUEST_BODY_BYTES, "member"),
        ("/upload", _UPLOAD_LIMIT, None),
    ],
)
async def test_a_credentialled_body_keeps_the_routes_full_limit(
    path: str, size: int, read: str | None
) -> None:
    headers, chunks = _body(path, size)

    result = await _request(
        _small_app(), "POST", path, chunks, headers={**headers, **_bearer("member")}
    )

    assert result.status == 200
    assert result.consumed == size
    if read is None:
        # The file is the whole body less its multipart envelope.
        assert 0 < result.json() < size
    else:
        assert result.json() == read


async def test_a_malformed_request_with_a_credential_is_answered_after_the_read() -> (
    None
):
    # The route's 422 for a path id that does not parse, as before, after the
    # body -- past the anonymous limit, within the route's -- is read.
    size = 4 * _ANONYMOUS

    result = await _request(
        _small_app(),
        "POST",
        "/items/nope",
        iter([_json_of(size)]),
        headers=_bearer("member", **_CHUNKED),
    )

    assert result.status == 422
    assert [e["loc"] for e in result.json()["errors"]] == [["path", "item"]]
    assert result.consumed == size


@pytest.mark.parametrize("path", ["/json", "/upload"])
async def test_a_caller_the_route_does_not_allow_is_answered_after_the_read(
    path: str,
) -> None:
    # A valid credential the route refuses: its 403 comes once the body, within
    # the route's limit, has been read (the epic's accepted trade-off, #3560).
    size = 4 * _ANONYMOUS
    headers, chunks = _body(path, size)

    result = await _request(
        _small_app(), "POST", path, chunks, headers={**headers, **_bearer("outsider")}
    )

    assert result.status == 403
    assert result.json()["reason"] == "forbidden"
    assert result.consumed == size


async def test_a_public_route_takes_an_anonymous_body() -> None:
    result = await _request(
        _small_app(), "POST", "/public", iter([b'{"value": "x"}']), headers=_CHUNKED
    )

    assert result.status == 200
    assert result.json() == "x"


async def test_an_overridden_dependency_decides_and_the_headers_set_the_limit() -> None:
    # ``dependency_overrides`` still decide the route's answer; the limit is the
    # headers', which a substitute for the route's check does not change.
    app = _small_app()
    app.dependency_overrides[_user] = lambda: "member"

    within = await _request(
        app, "POST", "/json", iter([_json_of(_ANONYMOUS)]), headers=_CHUNKED
    )
    past = await _request(app, "POST", "/json", _trickle(), headers=_CHUNKED)

    assert (within.status, within.json()) == (200, "member")
    assert past.status == 413
    assert past.consumed <= _ANONYMOUS + _TRICKLE


async def test_a_check_that_fails_unexpectedly_answers_after_the_read() -> None:
    size = 4 * _ANONYMOUS

    allowed = await _request(
        _small_app(),
        "POST",
        "/broken",
        iter([_json_of(size)]),
        headers=_bearer("member", **_CHUNKED),
    )
    anonymous = await _request(
        _small_app(), "POST", "/broken", _trickle(), headers=_CHUNKED
    )

    assert (allowed.status, allowed.consumed) == (500, size)
    assert allowed.json()["reason"] == "internal_error"
    assert anonymous.status == 413
    assert anonymous.consumed <= _ANONYMOUS + _TRICKLE


async def test_the_check_is_asked_once_per_request_at_its_first_body_read() -> None:
    calls = 0

    async def counting(request: Request) -> bool:
        nonlocal calls
        calls += 1
        return await _credentialled(request)

    app = FastAPI()
    install_problem_handlers(app)
    app.add_middleware(RequestBodyLimitMiddleware, credentialled=counting)

    @app.post("/upload")
    async def upload(file: UploadFile) -> int:
        return len(await file.read())

    @app.get("/plain")
    async def plain() -> bool:
        return True

    headers, chunks = _upload_of(4 * _ANONYMOUS)
    # Many reads of one body: one question.
    uploaded = await _request(
        app, "POST", "/upload", chunks, headers={**headers, **_bearer("member")}
    )
    assert (uploaded.status, calls) == (200, 1)

    # A request whose body is not read is not asked about.
    await _request(app, "GET", "/plain", iter([]), headers=_bearer("member"))
    assert calls == 1


async def test_a_client_that_closes_after_its_request_receives_the_413() -> None:
    # Over a real socket: a ``Connection: close`` client writing a body past the
    # anonymous limit. The rest is within what the server reads and drops, so
    # the client sees the answer rather than a reset.
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
    assert head.startswith(b"HTTP/1.1 413 ")
    assert json.loads(body) == _CREDENTIAL_REQUIRED


async def test_a_chunked_anonymous_body_with_a_length_beside_it_closes() -> None:
    # ``Content-Length: 0`` beside the transfer encoding is not believed: the
    # chunks are counted, refused past the limit, and the connection closes.
    size = _ANONYMOUS + 1
    async with _served(_small_app()) as port:
        head, body = await _exchange(
            port,
            b"POST /json HTTP/1.1\r\nHost: t\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n"
            + f"{size:x}\r\n".encode()
            + b"x" * size
            + b"\r\n",
        )

    assert head.startswith(b"HTTP/1.1 413 ")
    assert b"\r\nconnection: close" in head.lower()
    assert json.loads(body) == _CREDENTIAL_REQUIRED


# --- every shape a route's check can take -------------------------------------
#
# Whatever the route declares, its check runs where it always has, and the
# anonymous limit bounds what it reads first. An include-level check under a
# prefix is the case FastAPI 0.137 changed (issue #3470).


class _Gate:
    def __init__(self, user: Annotated[str, Depends(_user)]) -> None:
        self.user = user


async def _paged(user: Annotated[str, Depends(_user)], page: int = 1) -> str:
    # A check with a query parameter of its own.
    return user


async def _reads_the_body(user: Annotated[str, Depends(_user)], body: _Echo) -> str:
    # A check that reads the body it stands in front of: the pre-body check of
    # #3365 could not be made for it; now it simply runs after the read.
    assert body.value
    return user


async def _scoped(scopes: SecurityScopes, user: Annotated[str, Depends(_user)]) -> str:
    assert scopes.scopes == ["items:write"]
    return user


def _shaped(shape: str, *, limited: bool = True) -> tuple[FastAPI, list[str]]:
    """An app whose ``POST /outer/inner/items`` checks its caller as ``shape`` says.

    Returns the app and a log of the yielding dependency's enter and exit.
    """

    app = FastAPI()
    install_problem_handlers(app)
    if limited:
        app.add_middleware(RequestBodyLimitMiddleware, credentialled=_credentialled)
    log: list[str] = []

    async def session(user: Annotated[str, Depends(_user)]) -> AsyncIterator[str]:
        log.append("enter")
        try:
            yield user
        finally:
            log.append("exit")

    route_dependencies = {
        "depends": {"user": Depends(_user)},
        "security": {"user": Security(_scoped, scopes=["items:write"])},
        "class": {"user": Depends(_Gate)},
        "yield": {"user": Depends(session)},
        "query": {"user": Depends(_paged)},
        "body": {"user": Depends(_reads_the_body)},
    }
    inner = APIRouter(
        prefix="/inner",
        dependencies=[Depends(_user)] if shape == "router" else [],
    )

    if shape in route_dependencies:
        dependency = route_dependencies[shape]["user"]

        @inner.post("/items")
        async def items(body: _Echo, user: Any = dependency) -> str:
            return body.value

    else:

        @inner.post("/items", dependencies=[Depends(_user)] if shape == "route" else [])
        async def unparameterised(body: _Echo) -> str:
            return body.value

    outer = APIRouter(prefix="/outer")
    outer.include_router(
        inner, dependencies=[Depends(_user)] if shape == "include" else []
    )
    app.include_router(outer)
    return app, log


_SHAPES = [
    "depends",
    "security",
    "class",
    "yield",
    "query",
    "body",
    "route",
    "router",
    "include",
]


@pytest.mark.parametrize("shape", _SHAPES)
async def test_the_anonymous_limit_holds_whatever_shape_the_check_takes(
    shape: str,
) -> None:
    app, _ = _shaped(shape)

    refused = await _request(app, "POST", "/outer/inner/items", _trickle())
    assert refused.status == 413
    assert refused.consumed <= _ANONYMOUS + _TRICKLE

    # The #3470 probe: a 19-byte anonymous body gets the route's own 401.
    small = b'{"value": "abcdef"}'
    assert len(small) == 19
    answers = []
    for candidate in (app, _shaped(shape, limited=False)[0]):
        result = await _request(
            candidate, "POST", "/outer/inner/items", iter([small]), headers=_CHUNKED
        )
        answers.append((result.status, result.body))
    assert answers == [(401, _INVALID_TOKEN)] * 2

    allowed = await _request(
        app,
        "POST",
        "/outer/inner/items",
        iter([_json_of(4 * _ANONYMOUS)]),
        headers=_bearer("member", **_CHUNKED),
    )
    assert (allowed.status, allowed.json()) == (200, "x")


@pytest.mark.parametrize("token", [None, "member"])
async def test_a_check_that_yields_is_resolved_once(token: str | None) -> None:
    app, log = _shaped("yield")
    headers = _CHUNKED if token is None else _bearer(token, **_CHUNKED)

    await _request(
        app, "POST", "/outer/inner/items", iter([b'{"value": "x"}']), headers=headers
    )

    # Entered and left once, refused or allowed: nothing solves it a second time.
    assert log == ([] if token is None else ["enter", "exit"])


# --- the real check -------------------------------------------------------------


@pytest.fixture
async def live_app(shared_app: FastAPI) -> AsyncIterator[FastAPI]:
    """The real app with its lifespan open: its dependencies read ``app.state``."""

    async with shared_app.router.lifespan_context(shared_app):
        yield shared_app


_API = "/api/communities/{community_id}/servers"
_FILES = f"{_API}/{{server_id}}/files"


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)


def _signed(signing_key: str, *, issued: dt.datetime) -> str:
    tokens = JwtTokenService(
        signing_key=signing_key,
        algorithm="HS256",
        access_ttl=dt.timedelta(minutes=15),
        download_grant_ttl=dt.timedelta(minutes=1),
        download_cookie_ttl=dt.timedelta(minutes=1),
        clock=FakeClock(issued),
    )
    return tokens.issue_access_token(make_user().id)


def _test_signing_key(app: FastAPI) -> str:
    key = app.state.settings.auth.token.signing_key
    assert isinstance(key, str)
    return key


# A file write declaring past the route's limit, and past what is read and
# dropped after an answer: refused unread whoever sends it, but by the route's
# limit (``file_too_large``) only for a caller the check counts as credentialled.
_ROUTE_LIMIT = _problem("file_too_large")


async def _file_write(app: FastAPI, credential: dict[str, str]) -> Any:
    _sign_in(app)
    size = EARLY_ANSWER_DISCARD_BYTES + 1
    assert size > MAX_WRITE_BODY_BYTES
    result = await _request(
        app,
        "PUT",
        _concrete(_FILES),
        _endless(),
        headers=_declared(size, **credential),
    )
    assert (result.status, result.consumed) == (413, 0)
    return result.json()


@pytest.mark.parametrize(
    "kind", ["none", "basic", "empty", "nonsense", "expired", "foreign", "worker"]
)
async def test_the_real_check_counts_no_invalid_credential(
    live_app: FastAPI, kind: str
) -> None:
    long_ago = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    now = dt.datetime.now(dt.UTC)
    credential = {
        "none": {},
        "basic": {"authorization": "Basic bWVtYmVyOg=="},
        "empty": {"authorization": "Bearer "},
        "nonsense": _bearer("nonsense"),
        "expired": _bearer(_signed(_test_signing_key(live_app), issued=long_ago)),
        "foreign": _bearer(_signed("another-signing-key-of-32-bytes!", issued=now)),
        # No Worker credential is configured: none is accepted as one.
        "worker": _bearer("worker-secret"),
    }[kind]

    # Held to the anonymous limit, though the route (signed in by overrides)
    # would take the caller: no database lookup was needed to tell.
    assert await _file_write(live_app, credential) == _CREDENTIAL_REQUIRED


@pytest.mark.parametrize("kind", ["none", "nonsense", "expired"])
async def test_an_invalid_credential_within_the_limit_gets_the_routes_401(
    live_app: FastAPI, kind: str
) -> None:
    long_ago = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    credential = {
        "none": {},
        "nonsense": _bearer("nonsense"),
        "expired": _bearer(_signed(_test_signing_key(live_app), issued=long_ago)),
    }[kind]
    body = b'{"username": "bob"}'

    result = await _request(
        live_app,
        "PATCH",
        "/api/users/me",
        iter([body]),
        headers=_declared(len(body), **credential),
    )

    assert result.status == 401
    assert result.body == _INVALID_TOKEN
    assert result.headers["www-authenticate"] == "Bearer"
    assert result.consumed == len(body)


class _CountingTokens(FakeTokenService):
    def __init__(self) -> None:
        super().__init__()
        self.verified = 0

    def verify_access_token(self, token: str) -> VerifiedAccessToken:
        self.verified += 1
        return super().verify_access_token(token)


class _FailingUnitOfWork(FakeUnitOfWork):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def __aenter__(self) -> FakeUnitOfWork:
        raise self._error


def _authenticate_with(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI, use_case: AuthenticateRequest
) -> None:
    """Make both the body limit's check and ``get_current_user`` use ``use_case``."""

    async def authenticate(_request: object, token: str) -> Authentication:
        return await use_case(access_token=token)

    monkeypatch.setattr(dependencies, "_authenticate_body_token", authenticate)
    app.dependency_overrides[get_authenticate_request] = lambda: use_case


class _EchoProfile:
    """``UpdateProfile`` that returns the user it was asked to update, unchanged."""

    def __init__(self) -> None:
        self.user: User | None = None

    async def __call__(self, *, user_id: object, **_: object) -> User:
        assert self.user is not None and self.user.id == user_id
        return self.user


async def test_a_valid_token_keeps_the_routes_limit_and_is_verified_once(
    live_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = make_user()
    uow = FakeUnitOfWork()
    uow.users.seed(user)
    tokens = _CountingTokens()
    _authenticate_with(
        monkeypatch, live_app, AuthenticateRequest(uow=uow, tokens=tokens)
    )
    profile = _EchoProfile()
    profile.user = user
    live_app.dependency_overrides[get_update_profile] = lambda: profile
    credential = _bearer(tokens.issue_access_token(user.id))
    # Past the anonymous limit, within the route's (the default).
    size = 4 * _ANONYMOUS
    body = b'{"username": "alice"}'
    body += b" " * (size - len(body))

    # ``/api/users/me`` sits under included routers with a prefix: the user the
    # body limit verified reaches the route through the request's state.
    updated = await _request(
        live_app,
        "PATCH",
        "/api/users/me",
        iter([body]),
        headers=_declared(size, **credential),
    )

    assert updated.status == 200
    assert updated.json()["id"] == str(user.id.value)
    assert updated.consumed == size
    assert tokens.verified == 1

    # No body read, no check by the body limit: the route verifies on its own.
    fetched = await _request(
        live_app, "GET", "/api/users/me", iter([]), headers=credential
    )

    assert fetched.status == 200
    assert tokens.verified == 2


async def test_an_overridden_current_user_still_decides(
    live_app: FastAPI, body_credential: dict[str, str]
) -> None:
    # The body limit leaves a user on the request's state; an override of
    # ``get_current_user`` replaces the dependency, and with it that reuse.
    override = make_user(username="override")
    live_app.dependency_overrides[dependencies.get_current_user] = lambda: override
    profile = _EchoProfile()
    profile.user = override
    live_app.dependency_overrides[get_update_profile] = lambda: profile
    body = b'{"username": "x"}'

    result = await _request(
        live_app,
        "PATCH",
        "/api/users/me",
        iter([body]),
        headers=_declared(len(body), **body_credential),
    )

    assert result.status == 200
    assert result.json()["username"] == "override"


@pytest.mark.parametrize(
    ("error", "signed", "keeps_the_route_limit"),
    [
        # The database is unavailable: a signed token keeps its route's limit,
        # and the route's own authentication meets the outage after the read.
        (ConnectionRefusedError(), True, True),
        # A token that does not verify never reaches the database.
        (ConnectionRefusedError(), False, False),
        # Not an outage: a failure of the check is no credential, and no raise.
        (RuntimeError("a bug"), True, False),
    ],
)
async def test_a_user_lookup_that_fails(
    live_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    signed: bool,
    keeps_the_route_limit: bool,
) -> None:
    tokens = FakeTokenService()
    _authenticate_with(
        monkeypatch,
        live_app,
        AuthenticateRequest(uow=_FailingUnitOfWork(error), tokens=tokens),
    )
    token = tokens.issue_access_token(make_user().id) if signed else "nonsense"

    refusal = await _file_write(live_app, _bearer(token))

    assert refusal == (_ROUTE_LIMIT if keeps_the_route_limit else _CREDENTIAL_REQUIRED)


async def test_the_worker_credential_keeps_the_routes_limit(
    live_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dependencies, "get_worker_credential", lambda _r: "w-secret")

    assert await _file_write(live_app, _bearer("w-secret")) == _ROUTE_LIMIT
    assert await _file_write(live_app, _bearer("w-secreT")) == _CREDENTIAL_REQUIRED


# --- the wiring: every route ----------------------------------------------------

_BODYLESS_METHODS = {"get", "head", "options"}

# The routes the Worker credential, not a user's, opens. The schema shows their
# credential as a header parameter, not a security requirement.
_WORKER_ROUTES = {
    ("POST", "/api/data-plane/communities/{community_id}/servers/{server_id}/snapshot"),
}


def _largest_bodies() -> dict[tuple[str, str], bytes]:
    """The largest legitimate body of every route that takes an anonymous body.

    Strings are at their worst on the wire: an astral code point JSON-escaped as
    a surrogate pair (``ensure_ascii``), 12 bytes. A password is capped at 1024
    code points (``RegisterUserRequest``); a username and an email have no cap
    of their own and are taken at the same 1024 (an email address is at most 254
    octets, RFC 5321). A refresh token is ``token_urlsafe(32)``: 43 characters.
    """

    long = "\U0001f600" * 1024
    refresh = {"refresh_token": "A" * 43}
    return {
        ("POST", "/api/users"): json.dumps(
            {"username": long, "email": long + "@example.com", "password": long}
        ).encode(),
        ("POST", "/api/auth/login"): json.dumps(
            {"username": long, "password": long}
        ).encode(),
        ("POST", "/api/auth/refresh"): json.dumps(refresh).encode(),
        ("POST", "/api/auth/logout"): json.dumps(refresh).encode(),
        # Takes the refresh cookie, and no body.
        ("POST", "/api/auth/session"): b"",
    }


def _operations(app: FastAPI) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Every body-method operation of the route table, from the app's schema."""

    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            if method not in _BODYLESS_METHODS:
                yield method.upper(), path, operation


def test_every_route_that_takes_an_anonymous_body_fits_the_limit(
    shared_app: FastAPI,
) -> None:
    # A route that takes a request without a credential declares no security
    # requirement. A new one fails here until its largest legitimate body is
    # added above -- and if that does not fit the anonymous limit, the limit
    # has to be raised for it.
    unauthenticated = {
        (method, path)
        for method, path, operation in _operations(shared_app)
        if not operation.get("security")
    }
    largest = _largest_bodies()

    assert unauthenticated == set(largest) | _WORKER_ROUTES
    for route, body in largest.items():
        assert len(body) <= _ANONYMOUS, route
    # The figure the limit's comment (``http_body_limit.py``) is derived from.
    assert max(len(body) for body in largest.values()) == 36_921


class _Reached(Exception):
    """What a stand-in use case raises: the route got as far as calling it."""


class _StandIn:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *_: object, **__: object) -> None:
        self.calls += 1
        raise _Reached


# The use case behind each route, and whether its body reaches it: the session
# restore takes its refresh token from a cookie, which these requests carry none
# of, and answers 401 before it calls its use case.
_USE_CASES: dict[tuple[str, str], tuple[Any, bool]] = {
    ("POST", "/api/users"): (get_register_user, True),
    ("POST", "/api/auth/login"): (get_login, True),
    ("POST", "/api/auth/refresh"): (get_refresh_session, True),
    ("POST", "/api/auth/logout"): (get_logout, True),
    ("POST", "/api/auth/session"): (get_restore_session, False),
}


@pytest.mark.parametrize("route", sorted(_largest_bodies()))
async def test_every_anonymous_route_takes_its_largest_body(
    live_app: FastAPI, route: tuple[str, str]
) -> None:
    method, path = route
    body = _largest_bodies()[route]
    provider, reaches = _USE_CASES[route]
    stand_in = _StandIn()
    live_app.dependency_overrides[provider] = lambda: stand_in

    result = await _request(
        live_app, method, path, iter([body]), headers=_declared(len(body))
    )

    # Read whole and parsed: the route handed it to its use case.
    assert result.consumed == len(body)
    if reaches:
        assert (result.status, stand_in.calls) == (500, 1)
    else:
        assert result.status == 401


def _concrete_routes(app: FastAPI) -> Iterator[tuple[str, str]]:
    for method, path, _ in _operations(app):
        yield method, _concrete(path)


async def test_no_route_reads_more_than_the_anonymous_limit_without_a_credential(
    live_app: FastAPI,
) -> None:
    for method, path in sorted(_concrete_routes(live_app)):
        streamed = await _request(live_app, method, path, _trickle(), headers=_CHUNKED)
        declared = await _request(
            live_app,
            method,
            path,
            _endless(),
            headers=_declared(EARLY_ANSWER_DISCARD_BYTES + 1),
        )

        # Refused at the limit, or answered before a byte by a route that checks
        # its caller first; never read past it.
        assert streamed.status in (401, 413), (method, path)
        assert streamed.consumed <= _ANONYMOUS + _TRICKLE, (method, path)
        assert declared.status in (401, 413), (method, path)
        assert declared.consumed == 0, (method, path)


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
    # A body within the route's limit, past the anonymous one: this is the request
    # that used to be spooled to disk in full before its 401. It is now a 413
    # (the epic's accepted trade-off, #3560), before a byte is read.
    headers, chunks = _upload(MAX_UPLOAD_BYTES // 2)
    if credential is not None:
        headers["authorization"] = credential

    result = await _request(live_app, "POST", _concrete(path), chunks, headers=headers)

    assert result.status == 413
    assert result.json() == _CREDENTIAL_REQUIRED
    assert result.consumed == 0
    # Nor is the upload read and dropped afterwards: the connection closes.
    assert result.headers["connection"] == "close"


@pytest.mark.parametrize("credential", [None, "Bearer nonsense"])
async def test_a_json_write_without_a_valid_credential_is_cut_off_at_the_limit(
    live_app: FastAPI, credential: str | None
) -> None:
    headers = dict(_CHUNKED)
    if credential is not None:
        headers["authorization"] = credential

    result = await _request(
        live_app, "PUT", _concrete(_FILES), _trickle(), headers=headers
    )

    assert result.status == 413
    assert result.json() == _CREDENTIAL_REQUIRED
    assert result.consumed <= _ANONYMOUS + _TRICKLE


@pytest.mark.parametrize("path", [p for p in _UPLOADS if p.startswith(_API)])
@pytest.mark.parametrize(("member", "status"), [(False, 404), (True, 403)])
async def test_an_upload_the_caller_may_not_make_is_refused_after_the_read(
    live_app: FastAPI,
    body_credential: dict[str, str],
    path: str,
    member: bool,
    status: int,
) -> None:
    # Authenticated, but not a member of the community (404, no existence signal)
    # or without the permission on this server (403): the same two-layer answer,
    # now given once the body, within the route's limit, is read (#3560).
    _sign_in(live_app, member=member, allow=False)
    size = 4 * _ANONYMOUS
    headers, chunks = _upload_of(size)

    result = await _request(
        live_app,
        "POST",
        _concrete(path),
        chunks,
        headers={**headers, **body_credential},
    )

    assert result.status == status
    assert result.consumed == size


async def test_a_caller_the_route_allows_has_the_body_read(
    live_app: FastAPI, body_credential: dict[str, str]
) -> None:
    _sign_in(live_app)
    size = 4 * _ANONYMOUS

    result = await _request(
        live_app,
        "PATCH",
        _concrete("/api/communities/{community_id}"),
        _exactly(size),
        headers={**_CHUNKED, **body_credential},
    )

    # Read whole, and found not to be the JSON it should be.
    assert result.status == 422
    assert result.consumed == size


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
