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

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request, UploadFile
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.responses import StreamingResponse
from starlette.types import Message

from mc_server_dashboard_api.http_body_limit import (
    DEFAULT_MAX_REQUEST_BODY_BYTES,
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
        headers={"content-length": str(DEFAULT_MAX_REQUEST_BODY_BYTES + 1)},
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


def test_the_write_body_limit_carries_a_file_at_the_edit_cap() -> None:
    # 4 * ceil(n / 3) characters of base64 for n bytes, plus the JSON around it.
    assert MAX_EDIT_BASE64_CHARS == 4 * -(-MAX_EDIT_BYTES // 3) == 5_592_408
    envelope = len(json.dumps({"content_base64": ""}))
    assert MAX_WRITE_BODY_BYTES >= MAX_EDIT_BASE64_CHARS + envelope


async def test_the_real_app_refuses_an_endless_file_write_within_its_limit(
    shared_app: FastAPI,
) -> None:
    # The memory-shaped case of the issue: PUT .../files used to buffer and parse
    # whatever it was sent before checking MAX_EDIT_BYTES. No credentials are
    # needed to show it -- the body is read before the route's dependencies run.
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
