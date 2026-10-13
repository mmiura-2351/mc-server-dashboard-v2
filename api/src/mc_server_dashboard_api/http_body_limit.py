"""A bound on how much request body a route reads (issue #3363).

Nothing in front of the routes limits a request body: uvicorn has no such
setting, and a route's own cap applies only after the framework has buffered the
whole JSON body, or spooled the whole multipart body to disk, to hand it over. So
the size of what one request could make the API hold was the client's choice.

:class:`RequestBodyLimitMiddleware` wraps the ASGI ``receive`` channel and refuses
a body past the route's limit with a 413 problem response:

* by ``Content-Length``, before a byte is read; and
* by streamed size, as soon as the running count crosses the limit — which is
  what bounds a chunked body, or one that under-declares its length.

The limit is :data:`DEFAULT_MAX_REQUEST_BODY_BYTES` unless the route declares its
own with :func:`request_body_limit`. The declaration sits on the endpoint, beside
the cap it derives from, and the middleware finds it through the ``endpoint`` the
router records in the request scope: by the time a route reads its body, routing
has happened. A request no route matched has the default.

It bounds what a route **reads**, and nothing else. A body the route never asks
for is never buffered (uvicorn applies flow control and discards it), so ``GET``,
``HEAD`` and ``OPTIONS`` requests — which no route reads a body for — are not
wrapped at all, and once a response has started the middleware stands aside.

It is the innermost middleware (``app.py``), and it refuses by raising: the
exception surfaces in the route's own body read, and the problem handlers render
it like any other error, so the 413 leaves through every other middleware with
the headers every response carries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mc_server_dashboard_api.http_body_auth import authorize_before_body
from mc_server_dashboard_api.http_problem import problem

# The limit of a route that declares none: every JSON route. The largest
# legitimate JSON body is a server's ``config`` blob, itself capped at 64 KiB
# (``MAX_CONFIG_BYTES``), so 1 MiB is ample.
DEFAULT_MAX_REQUEST_BODY_BYTES = 1024 * 1024
DEFAULT_REASON = "request_too_large"

# What a multipart upload route allows on top of its file cap for the envelope:
# the boundaries, the part headers and the route's few short text fields. It keeps
# a file of exactly the cap readable, so the route's own check still decides
# there.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024

_ATTRIBUTE = "__mcsd_request_body_limit__"

# Methods no route reads a body for. They are passed through untouched: their
# routes include the streaming downloads, and a streaming response calls
# ``receive`` itself, to watch for the client going away, before its first byte
# is sent -- a call this middleware could not tell from a route reading its body.
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_Endpoint = TypeVar("_Endpoint", bound=Callable[..., Any])


@dataclass(frozen=True)
class RequestBodyLimit:
    """The most body a route reads, and the 413 ``reason`` past it."""

    max_bytes: int
    reason: str


_DEFAULT = RequestBodyLimit(DEFAULT_MAX_REQUEST_BODY_BYTES, DEFAULT_REASON)


def request_body_limit(
    max_bytes: int, *, reason: str = DEFAULT_REASON
) -> Callable[[_Endpoint], _Endpoint]:
    """Declare a route's request-body limit, in place of the default.

    ``reason`` is the problem reason of the 413; a route that already refuses an
    over-size payload names the same reason here, so a client sees one answer
    however far past the cap its body was.
    """

    if max_bytes <= 0:
        raise ValueError("a request body limit must be positive")
    limit = RequestBodyLimit(max_bytes, reason)

    def declare(endpoint: _Endpoint) -> _Endpoint:
        setattr(endpoint, _ATTRIBUTE, limit)
        return endpoint

    return declare


def request_body_limit_of(endpoint: object) -> RequestBodyLimit:
    """The limit ``endpoint`` declared, or the default."""

    limit = getattr(endpoint, _ATTRIBUTE, _DEFAULT)
    assert isinstance(limit, RequestBodyLimit)
    return limit


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class RequestBodyLimitMiddleware:
    """Refuse a request body past its route's limit (module docstring)."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in _BODYLESS_METHODS:
            await self._app(scope, receive, send)
            return

        declared = _declared_length(scope)
        received = 0
        authorized = False
        responding = False

        async def limited_receive() -> Message:
            nonlocal received, authorized
            if responding:
                return await receive()
            if not authorized:
                await authorize_before_body(scope)
                authorized = True
            limit = request_body_limit_of(scope.get("endpoint"))
            if declared is not None and declared > limit.max_bytes:
                raise problem(status.HTTP_413_CONTENT_TOO_LARGE, limit.reason)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit.max_bytes:
                    raise problem(status.HTTP_413_CONTENT_TOO_LARGE, limit.reason)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal responding
            if message["type"] == "http.response.start":
                responding = True
            await send(message)

        await self._app(scope, limited_receive, tracking_send)
