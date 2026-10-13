"""Tests for the request-body size limit (issue #3363).

Nothing in front of a route bounded how much request body it would read: uvicorn
has no limit, and the routes' own caps apply only once the whole body has been
buffered (JSON) or spooled to disk (multipart). ``RequestBodyLimitMiddleware``
bounds what a route can read; these tests pin

- the mechanism, on a small app: the default and a per-route override, exactly at
  the limit and one byte past it, refused by ``Content-Length`` before a byte is
  read and by streamed size as soon as the count crosses the limit;
- the wiring, on the real app: the limit every body-accepting route ends up with
  (the inventory), and that the refusal is an ordinary problem response carrying
  the headers every other response does.

The requests are driven as raw ASGI calls where the point is how much of the body
the app *consumed*, which no HTTP client reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request, UploadFile
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.responses import StreamingResponse
from starlette.types import Message

from mc_server_dashboard_api import http_body_limit
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.dependencies import (
    get_current_user,
    get_membership_visibility,
    get_permission_checker,
)
from mc_server_dashboard_api.http_body_limit import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    EARLY_ANSWER_DISCARD_BYTES,
    MULTIPART_OVERHEAD_BYTES,
    RequestBodyLimitMiddleware,
    request_body_limit,
    request_body_limit_of,
)
from mc_server_dashboard_api.http_problem import install_problem_handlers
from mc_server_dashboard_api.servers.api.files import (
    MAX_EDIT_BASE64_CHARS,
    MAX_WRITE_BODY_BYTES,
)
from mc_server_dashboard_api.servers.application.files import (
    MAX_EDIT_BYTES,
    MAX_UPLOAD_BYTES,
)
from mc_server_dashboard_api.servers.application.plugins import MAX_PLUGIN_BYTES
from mc_server_dashboard_api.servers.application.resource_packs import (
    MAX_RESOURCE_PACK_BYTES,
)
from tests.identity.fakes import make_user

_CHUNK = 64 * 1024


class _Result:
    def __init__(self) -> None:
        self.status = 0
        self.headers: dict[str, str] = {}
        self.body = b""
        # Body bytes the app pulled out of ``receive`` -- what it could have held.
        self.consumed = 0

    def json(self) -> Any:
        return json.loads(self.body)


async def _request(
    app: Any,
    method: str,
    path: str,
    chunks: Iterator[bytes],
    *,
    headers: dict[str, str] | None = None,
) -> _Result:
    """Run one request through ``app`` as a raw ASGI call, feeding ``chunks`` lazily."""

    result = _Result()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (name.lower().encode(), value.encode())
            for name, value in (headers or {}).items()
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    done = False

    async def receive() -> Message:
        nonlocal done
        if done:
            return {"type": "http.disconnect"}
        chunk = next(chunks, None)
        if chunk is None:
            done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        result.consumed += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            result.status = message["status"]
            result.headers = {
                name.decode(): value.decode() for name, value in message["headers"]
            }
        elif message["type"] == "http.response.body":
            result.body += message.get("body", b"")

    await app(scope, receive, send)
    return result


def _endless() -> Iterator[bytes]:
    """A body far larger than any limit under test: only a limit stops the read.

    It gives out at 64 MiB with a failure rather than running on, so a missing
    limit is a red test and not a process that eats the host's memory.
    """

    for _ in range(64 * 1024 * 1024 // _CHUNK):
        yield b"x" * _CHUNK
    raise AssertionError("the app read 64 MiB of a body it should have refused")


def _exactly(size: int) -> Iterator[bytes]:
    whole, rest = divmod(size, _CHUNK)
    for _ in range(whole):
        yield b"x" * _CHUNK
    if rest:
        yield b"x" * rest


def _problem(reason: str) -> dict[str, object]:
    return {
        "type": f"urn:mcsd:error:{reason}",
        "title": "Content Too Large",
        "status": 413,
        "reason": reason,
    }


# --- the mechanism ----------------------------------------------------------

_OVERRIDE = 3 * DEFAULT_MAX_REQUEST_BODY_BYTES


class _Echo(BaseModel):
    value: str


def _small_app() -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)
    app.add_middleware(RequestBodyLimitMiddleware)

    @app.post("/default")
    async def default(request: Request) -> dict[str, int]:
        return {"read": len(await request.body())}

    @app.post("/override")
    @request_body_limit(_OVERRIDE, reason="file_too_large")
    async def override(request: Request) -> dict[str, int]:
        return {"read": len(await request.body())}

    @app.post("/json")
    async def parsed(body: _Echo) -> dict[str, int]:
        return {"read": len(body.value)}

    @app.post("/multipart")
    @request_body_limit(_OVERRIDE, reason="too_large")
    async def multipart(file: UploadFile) -> dict[str, int]:
        return {"read": len(await file.read())}

    @app.get("/ignores-body")
    async def ignores_body() -> StreamingResponse:
        async def content() -> Any:
            yield b"ok"

        return StreamingResponse(content())

    return app


@pytest.mark.parametrize(
    ("path", "limit"),
    [("/default", DEFAULT_MAX_REQUEST_BODY_BYTES), ("/override", _OVERRIDE)],
)
async def test_a_body_of_exactly_the_limit_is_read_whole(path: str, limit: int) -> None:
    result = await _request(_small_app(), "POST", path, _exactly(limit))

    assert result.status == 200
    assert result.json() == {"read": limit}


@pytest.mark.parametrize(
    ("path", "limit", "reason"),
    [
        ("/default", DEFAULT_MAX_REQUEST_BODY_BYTES, "request_too_large"),
        ("/override", _OVERRIDE, "file_too_large"),
    ],
)
async def test_one_byte_past_the_limit_is_413(
    path: str, limit: int, reason: str
) -> None:
    result = await _request(_small_app(), "POST", path, _exactly(limit + 1))

    assert result.status == 413
    assert result.headers["content-type"] == "application/problem+json"
    assert result.json() == _problem(reason)


async def test_a_streamed_body_is_cut_off_as_the_count_crosses_the_limit() -> None:
    # No Content-Length (a chunked upload), and a body that would never end: the
    # route stops reading within one chunk of its limit instead of buffering it.
    result = await _request(_small_app(), "POST", "/default", _endless())

    assert result.status == 413
    assert result.json() == _problem("request_too_large")
    assert result.consumed <= DEFAULT_MAX_REQUEST_BODY_BYTES + _CHUNK


async def test_a_declared_length_past_the_limit_is_refused_unread() -> None:
    result = await _request(
        _small_app(),
        "POST",
        "/default",
        _endless(),
        headers={"content-length": str(EARLY_ANSWER_DISCARD_BYTES + 1)},
    )

    assert result.status == 413
    assert result.json() == _problem("request_too_large")
    assert result.consumed == 0


async def test_a_declared_length_of_exactly_the_limit_is_read() -> None:
    result = await _request(
        _small_app(),
        "POST",
        "/default",
        _exactly(DEFAULT_MAX_REQUEST_BODY_BYTES),
        headers={"content-length": str(DEFAULT_MAX_REQUEST_BODY_BYTES)},
    )

    assert result.status == 200
    assert result.json() == {"read": DEFAULT_MAX_REQUEST_BODY_BYTES}


async def test_a_body_that_under_declares_its_length_is_still_counted() -> None:
    result = await _request(
        _small_app(),
        "POST",
        "/default",
        _endless(),
        headers={"content-length": "10"},
    )

    assert result.status == 413
    assert result.consumed <= DEFAULT_MAX_REQUEST_BODY_BYTES + _CHUNK


async def test_a_json_body_past_the_limit_is_413_not_a_parse_error() -> None:
    # FastAPI reads a declared body itself and turns a failure to do so into a
    # 400; the limit's refusal has to come through that as the 413 it is.
    body = json.dumps({"value": "x" * DEFAULT_MAX_REQUEST_BODY_BYTES}).encode()
    result = await _request(
        _small_app(),
        "POST",
        "/json",
        iter([body]),
        headers={"content-type": "application/json"},
    )

    assert result.status == 413
    assert result.json() == _problem("request_too_large")


def _multipart(size: int) -> tuple[dict[str, str], Iterator[bytes]]:
    head = (
        b"--b\r\n"
        b'Content-Disposition: form-data; name="file"; filename="f.bin"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
    )
    tail = b"\r\n--b--\r\n"

    def chunks() -> Iterator[bytes]:
        yield head
        yield from _exactly(size)
        yield tail

    return {"content-type": "multipart/form-data; boundary=b"}, chunks()


async def test_a_multipart_upload_within_its_limit_is_parsed() -> None:
    headers, chunks = _multipart(_OVERRIDE - 1024)

    result = await _request(_small_app(), "POST", "/multipart", chunks, headers=headers)

    assert result.status == 200
    assert result.json() == {"read": _OVERRIDE - 1024}


async def test_a_multipart_upload_past_its_limit_stops_being_spooled() -> None:
    headers, _ = _multipart(0)
    head = (
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="f.bin"\r\n\r\n'
    )

    def chunks() -> Iterator[bytes]:
        yield head
        yield from _endless()

    result = await _request(
        _small_app(), "POST", "/multipart", chunks(), headers=headers
    )

    assert result.status == 413
    assert result.json() == _problem("too_large")
    assert result.consumed <= _OVERRIDE + _CHUNK


async def test_a_route_that_never_reads_its_body_is_left_alone() -> None:
    # The limit bounds what a route READS. A streaming response calls receive
    # itself, to watch for the client going away; a download is not torn up
    # because its GET declared a body nobody asked for.
    result = await _request(
        _small_app(),
        "GET",
        "/ignores-body",
        _exactly(10 * DEFAULT_MAX_REQUEST_BODY_BYTES),
        headers={"content-length": str(10 * DEFAULT_MAX_REQUEST_BODY_BYTES)},
    )

    assert result.status == 200
    assert result.body == b"ok"


# --- an answer given before the body was read through --------------------------
#
# A refusal -- over-size, or the route's credential check (issue #3365) -- leaves
# the rest of the body unread, with the client possibly still sending it. Either
# the rest is small enough to read and drop, so the client finishes and sees the
# answer on a connection that stays usable, or the answer says the connection is
# closing and nothing more is read.

_REST = 4 * DEFAULT_MAX_REQUEST_BODY_BYTES


async def test_a_refusal_drops_the_rest_of_a_small_body() -> None:
    result = await _request(
        _small_app(),
        "POST",
        "/default",
        _endless(),
        headers={"content-length": str(_REST)},
    )

    assert result.status == 413
    assert result.json() == _problem("request_too_large")
    # Read to its declared end and no further; none of it reached the route.
    assert _REST <= result.consumed < _REST + _CHUNK
    assert "connection" not in result.headers


async def test_a_refusal_closes_rather_than_read_a_large_rest() -> None:
    result = await _request(
        _small_app(),
        "POST",
        "/default",
        _endless(),
        headers={"content-length": str(EARLY_ANSWER_DISCARD_BYTES + 1)},
    )

    assert result.status == 413
    assert result.consumed == 0
    assert result.headers["connection"] == "close"


async def test_a_refusal_closes_when_the_rest_has_no_declared_end() -> None:
    result = await _request(
        _small_app(),
        "POST",
        "/default",
        _endless(),
        headers={"transfer-encoding": "chunked"},
    )

    assert result.status == 413
    assert result.consumed <= DEFAULT_MAX_REQUEST_BODY_BYTES + _CHUNK
    assert result.headers["connection"] == "close"


async def test_a_refusal_does_not_invite_a_body_the_client_is_holding_back() -> None:
    # ``Expect: 100-continue``: the client sends its body only once the server
    # asks for it, and the first read is what asks.
    result = await _request(
        _small_app(),
        "POST",
        "/default",
        _endless(),
        headers={
            "content-length": str(DEFAULT_MAX_REQUEST_BODY_BYTES + 1),
            "expect": "100-continue",
        },
    )

    assert result.status == 413
    assert result.consumed == 0
    assert result.headers["connection"] == "close"


async def test_a_refusal_stops_waiting_for_a_rest_that_does_not_come(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_body_limit, "EARLY_ANSWER_DISCARD_SECONDS", 0.05)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/default",
        "raw_path": b"/default",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-length", str(_REST).encode())],
    }
    start: dict[str, Any] = {}

    async def receive() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            start.update(message)

    await _small_app()(scope, receive, send)

    assert start["status"] == 413
    assert (b"connection", b"close") in start["headers"]


async def test_a_body_read_through_is_answered_as_before() -> None:
    result = await _request(
        _small_app(),
        "POST",
        "/default",
        _exactly(1024),
        headers={"content-length": "1024"},
    )

    assert result.status == 200
    assert result.consumed == 1024
    assert "connection" not in result.headers


class _Server(uvicorn.Server):
    """A uvicorn server that leaves the test process's signal handlers alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@contextlib.asynccontextmanager
async def _served(app: Any) -> AsyncIterator[int]:
    """Serve ``app`` with the production server on a loopback port of the OS's choice.

    What a client receives when the server answers before it has read the body
    is decided by the server's socket handling, which no in-process ASGI call
    shows.
    """

    server = _Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            http="h11",
            ws="none",
            lifespan="off",
            log_config=None,
            # Long, so a connection that closes soon after an answer was closed
            # by the answer and not by idling out.
            timeout_keep_alive=60,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        await task


async def test_a_client_that_closes_after_its_request_receives_the_refusal() -> None:
    # The lost 413 of the issue: a ``Connection: close`` client still writing
    # its body when the server answered and closed was reset, and never saw the
    # answer. The rest of this body is within what the server reads and drops.
    async with _served(_small_app()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /default HTTP/1.1\r\nHost: t\r\nConnection: close\r\n"
            + f"Content-Length: {_REST}\r\n\r\n".encode()
        )
        for _ in range(_REST // _CHUNK):
            writer.write(b"x" * _CHUNK)
            await writer.drain()

        response = await asyncio.wait_for(reader.read(), timeout=10)
        writer.close()

    head, _, body = response.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 413 ")
    assert json.loads(body) == _problem("request_too_large")


async def test_a_refusal_of_a_large_declared_body_closes_the_connection() -> None:
    # The connection used to stay open for as long as the client took to send
    # the length it had declared, all of it read and thrown away.
    async with _served(_small_app()) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /default HTTP/1.1\r\nHost: t\r\n"
            + f"Content-Length: {EARLY_ANSWER_DISCARD_BYTES + 1}\r\n\r\n".encode()
        )
        await writer.drain()

        # To end of stream: the server closed, with none of the body sent.
        response = await asyncio.wait_for(reader.read(), timeout=10)
        writer.close()

    head, _, body = response.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 413 ")
    assert b"\r\nconnection: close" in head.lower()
    assert json.loads(body) == _problem("request_too_large")


def test_a_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        request_body_limit(0)


# --- the wiring: every body-accepting route's limit ---------------------------

# The routes whose limit is not the default, as (method, path) -> (bytes, reason).
# Each is the route's own cap plus what its envelope adds, so the route's own
# check still decides at the cap; the limit only stops a body far past it from
# being buffered or spooled first.
_API = "/api/communities/{community_id}/servers"
_UPLOAD_LIMIT = MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES
_EXPECTED_OVERRIDES: dict[tuple[str, str], tuple[int, str]] = {
    ("PUT", f"{_API}/{{server_id}}/files"): (MAX_WRITE_BODY_BYTES, "file_too_large"),
    ("POST", f"{_API}/{{server_id}}/files/upload"): (_UPLOAD_LIMIT, "file_too_large"),
    ("POST", f"{_API}/import"): (_UPLOAD_LIMIT, "file_too_large"),
    ("POST", f"{_API}/{{server_id}}/backups/upload"): (_UPLOAD_LIMIT, "too_large"),
    ("POST", f"{_API}/{{server_id}}/plugins"): (
        MAX_PLUGIN_BYTES + MULTIPART_OVERHEAD_BYTES,
        "file_too_large",
    ),
    ("POST", "/api/resource-packs"): (
        MAX_RESOURCE_PACK_BYTES + MULTIPART_OVERHEAD_BYTES,
        "too_large",
    ),
    (
        "POST",
        "/api/data-plane/communities/{community_id}/servers/{server_id}/snapshot",
    ): (
        50 * 1024 * 1024 * 1024,
        "snapshot_too_large",
    ),
}


def test_every_route_has_the_limit_the_inventory_says(shared_app: FastAPI) -> None:
    overrides: dict[tuple[str, str], tuple[int, str]] = {}
    for route in shared_app.routes:
        if not isinstance(route, APIRoute):
            continue
        limit = request_body_limit_of(route.endpoint)
        if (limit.max_bytes, limit.reason) == (
            DEFAULT_MAX_REQUEST_BODY_BYTES,
            "request_too_large",
        ):
            continue
        for method in route.methods:
            overrides[(method, route.path)] = (limit.max_bytes, limit.reason)

    assert overrides == _EXPECTED_OVERRIDES


class _Visibility(MembershipVisibility):
    def __init__(self, *, member: bool) -> None:
        self._member = member

    async def is_member(self, **_: Any) -> bool:
        return self._member


class _Checker(PermissionChecker):
    def __init__(self, *, allow: bool) -> None:
        self._allow = allow

    async def can(self, **_: Any) -> bool:
        return self._allow


def _sign_in(app: FastAPI, *, member: bool = True, allow: bool = True) -> None:
    """Make every request to ``app`` an authenticated one, allowed as given.

    A route checks its caller before it reads a body (issue #3365), so a test of
    what a route does with a body has to get past that first.
    """

    app.dependency_overrides[get_current_user] = make_user
    app.dependency_overrides[get_membership_visibility] = lambda: _Visibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _Checker(allow=allow)


def test_the_write_body_limit_carries_a_file_at_the_edit_cap() -> None:
    # 4 * ceil(n / 3) characters of base64 for n bytes. On the wire each is one
    # byte, or two where an encoder escapes "/" as "\/" -- at worst all of them
    # -- plus 64 KiB for the JSON around the string (test_file_endpoints.py
    # drives both serializations through the route).
    assert MAX_EDIT_BASE64_CHARS == 4 * -(-MAX_EDIT_BYTES // 3) == 5_592_408
    assert MAX_WRITE_BODY_BYTES == 2 * 5_592_408 + 64 * 1024 == 11_250_352


async def test_the_real_app_refuses_an_endless_file_write_within_its_limit(
    shared_app: FastAPI,
) -> None:
    # The memory-shaped case of the issue: PUT .../files used to buffer and parse
    # whatever it was sent before checking MAX_EDIT_BYTES.
    _sign_in(shared_app)
    path = (
        "/api/communities/00000000-0000-0000-0000-000000000001"
        "/servers/00000000-0000-0000-0000-000000000002/files"
    )

    result = await _request(
        shared_app,
        "PUT",
        path,
        _endless(),
        headers={"content-type": "application/json"},
    )

    assert result.status == 413
    assert result.headers["content-type"] == "application/problem+json"
    assert result.json() == _problem("file_too_large")
    assert result.consumed <= MAX_WRITE_BODY_BYTES + _CHUNK
    # An ordinary response: it went out through every middleware.
    assert result.headers["x-correlation-id"]
    assert result.headers["x-content-type-options"] == "nosniff"


async def test_the_real_app_refuses_an_endless_json_body_at_the_default(
    shared_app: FastAPI,
) -> None:
    result = await _request(
        shared_app,
        "POST",
        "/api/auth/login",
        _endless(),
        headers={"content-type": "application/json"},
    )

    assert result.status == 413
    assert result.json() == _problem("request_too_large")
    assert result.consumed <= DEFAULT_MAX_REQUEST_BODY_BYTES + _CHUNK
