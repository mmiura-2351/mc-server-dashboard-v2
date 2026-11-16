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

A request that carries ``Transfer-Encoding`` has no declared length, whatever
``Content-Length`` it also carries: the server (h11) frames such a body as
chunked and ignores the length, so nothing here believes it either.

The limit is :data:`DEFAULT_MAX_REQUEST_BODY_BYTES` unless the route declares its
own with :func:`request_body_limit`. The declaration sits on the endpoint, beside
the cap it derives from, and the middleware finds it through the ``endpoint`` the
router records in the request scope: by the time a route reads its body, routing
has happened. A request no route matched has the default.

It bounds what a route **reads**, and nothing else. A body the route never asks
for is never buffered (uvicorn applies flow control and discards it). ``GET``,
``HEAD`` and ``OPTIONS`` requests — which no route reads a body for — are neither
classified nor limited here: their routes include the streaming downloads, and a
streaming response calls ``receive`` itself, to watch for the client going away,
before its first byte is sent — a call that cannot be told from a route reading
its body. Once a response has started the middleware stands aside.

It is the innermost middleware (``app.py``), and it refuses by raising: the
exception surfaces in the route's own body read, and the problem handlers render
it like any other error, so the 413 leaves through every other middleware with
the headers every response carries.

**A body without a credential is held to a small limit** (issues #3365, #3561).
Routes check their caller in their own dependencies, and FastAPI solves those
only after it has read the declared body. So, at a request's first body read,
the middleware asks the ``credentialled`` check it is given whether the request
carries a valid credential, from its headers alone (in the app,
:func:`mc_server_dashboard_api.dependencies.has_body_credential`: a valid user
access token or the Worker credential). A request that does keeps its route's
limit; one that does not is held to :data:`ANONYMOUS_MAX_REQUEST_BODY_BYTES`, or
the route's limit if that is smaller, and refused past it with a 413 whose
``reason`` is :data:`ANONYMOUS_REASON` (by ``Content-Length`` before a byte is
read, or by running count). Within that, its
body is read and the route answers it as it always has -- the 401, and any 403
or 404, come from the route's dependencies after the read. So a caller without
a credential cannot make the API read more than the anonymous limit, and no
route changes how it receives its request or keeps a list of public routes.

**An answer given before the body was read through** (issue #3365) -- either
refusal above, or any response to a request whose body was not read: a route
that declares none, a request no route matched, any method -- finds the client
possibly still sending. The server would go on reading and
dropping whatever length the client declared, for as long as the client took;
and for a client that asked for the connection to close, it closes with the body
still arriving, which resets the connection and can cost the client the answer.
So, when a response starts:

* an unread rest of at most :data:`EARLY_ANSWER_DISCARD_BYTES` is read and
  dropped first, for at most :data:`EARLY_ANSWER_DISCARD_SECONDS`: the client
  finishes its request, receives the answer, and the connection stays usable;
* a longer rest, one of undeclared length (a transfer-encoded body), one that
  did not arrive in that time, one the client is holding back for a
  ``100 Continue``, or any rest at all on a ``GET``, ``HEAD`` or ``OPTIONS``
  (reading it here would race the streaming response's own watch on ``receive``)
  is not read: the response carries ``Connection: close`` and the server closes
  once it is sent. A client still writing then may see a reset before it reads
  the answer; that is the price of not reading the body, and it is paid only by
  bodies past the discard budget.

A request with no body -- neither ``Content-Length`` nor ``Transfer-Encoding``,
or a length of zero -- has nothing unread, and its response is never touched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import status
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mc_server_dashboard_api.http_problem import problem

# The limit of a route that declares none: every JSON route. The largest
# legitimate JSON body is a server's ``config`` blob, itself capped at 64 KiB
# (``MAX_CONFIG_BYTES``), so 1 MiB is ample.
DEFAULT_MAX_REQUEST_BODY_BYTES = 1024 * 1024
DEFAULT_REASON = "request_too_large"

# The limit of a request without a valid credential (module docstring), sized
# for the routes that take a body from such a caller, as the route table has
# them: registration, login, refresh and logout (``POST /api/auth/session``
# takes none). ``test_http_body_credential.py`` derives that set from the table
# and pins each route's largest legitimate body. The largest is a registration
# of 36,921 bytes: a password at its 1024-code-point cap
# (``RegisterUserRequest``), at worst 12 bytes each on the wire (an astral code
# point JSON-escaped as a surrogate pair), and a username and an email, which
# have no cap of their own, taken at the same 1024 code points -- far past any
# real login name or address (RFC 5321 bounds an address at 254 octets). A
# login is 24,608 bytes; a refresh or a logout carries a 43-character token.
# This limit is now also the bound on those uncapped fields for such a caller.
ANONYMOUS_MAX_REQUEST_BODY_BYTES = 64 * 1024
# The 413 ``reason`` past it: not the route's own cap (``request_too_large``)
# but the want of a credential, so a client whose access token has expired can
# tell the two apart, refresh, and send the body again -- as it does on a 401.
ANONYMOUS_REASON = "credential_required_for_body"

# What a multipart upload route allows on top of its file cap for the envelope:
# the boundaries, the part headers and the route's few short text fields. It keeps
# a file of exactly the cap readable, so the route's own check still decides
# there.
MULTIPART_OVERHEAD_BYTES = 1024 * 1024

# How much unread body is read and dropped, and for how long, so that an answer
# given before the body was read through reaches a client still sending it
# (module docstring). 16 MiB covers a refused request to any JSON route, the
# largest being a file edit a little past its 11 MB limit; an upload is larger
# and is answered by closing instead.
EARLY_ANSWER_DISCARD_BYTES = 16 * 1024 * 1024
EARLY_ANSWER_DISCARD_SECONDS = 10.0

_ATTRIBUTE = "__mcsd_request_body_limit__"

# Methods no route reads a body for (module docstring): a ``receive`` call on
# them is not a body read, so it is neither classified nor limited, and a body
# they do carry is never read and dropped.
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_Endpoint = TypeVar("_Endpoint", bound=Callable[..., Any])


@dataclass(frozen=True)
class RequestBodyLimit:
    """The most body a route reads, and the 413 ``reason`` past it."""

    max_bytes: int
    reason: str


_DEFAULT = RequestBodyLimit(DEFAULT_MAX_REQUEST_BODY_BYTES, DEFAULT_REASON)
_ANONYMOUS = RequestBodyLimit(ANONYMOUS_MAX_REQUEST_BODY_BYTES, ANONYMOUS_REASON)

# Whether a request carries a valid credential, judged from its headers alone
# (module docstring). It answers, and never raises.
CredentialCheck = Callable[[Request], Awaitable[bool]]


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


def _declared_length(headers: dict[bytes, bytes]) -> int | None:
    """The body's length, if the request's framing declares one."""

    if b"transfer-encoding" in headers:
        return None
    try:
        return int(headers[b"content-length"])
    except (KeyError, ValueError):
        return None


async def _discard(receive: Receive, rest: int | None) -> bool:
    """Read and drop ``rest`` unread body bytes; False if they were left unread."""

    if rest is None or rest > EARLY_ANSWER_DISCARD_BYTES:
        return False
    if rest <= 0:
        # Nothing unread -- nearly every request: no read, and no deadline armed.
        return True
    try:
        async with asyncio.timeout(EARLY_ANSWER_DISCARD_SECONDS):
            while rest > 0:
                message = await receive()
                if message["type"] != "http.request":
                    break
                rest -= len(message.get("body", b""))
                if not message.get("more_body", False):
                    break
    except TimeoutError:
        return False
    return True


class RequestBodyLimitMiddleware:
    """Refuse a request body past its route's limit (module docstring)."""

    def __init__(self, app: ASGIApp, *, credentialled: CredentialCheck) -> None:
        self._app = app
        self._credentialled = credentialled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        reads_a_body = scope["method"] not in _BODYLESS_METHODS
        headers = dict(scope["headers"])
        declared = _declared_length(headers)
        received = 0
        ended = False
        limit: RequestBodyLimit | None = None
        responding = False

        def unread() -> int | None:
            """How much body is still to come; ``None`` when that is not to be read."""

            if ended:
                return 0
            if declared is None:
                return None if b"transfer-encoding" in headers else 0
            if declared == received:
                return 0
            if not reads_a_body:
                return None
            if received == 0 and headers.get(b"expect", b"").lower() == b"100-continue":
                return None
            return declared - received

        async def limited_receive() -> Message:
            nonlocal received, ended, limit
            if responding:
                return await receive()
            if reads_a_body:
                if limit is None:
                    limit = request_body_limit_of(scope.get("endpoint"))
                    if limit.max_bytes > _ANONYMOUS.max_bytes and not (
                        await self._credentialled(Request(scope))
                    ):
                        limit = _ANONYMOUS
                if declared is not None and declared > limit.max_bytes:
                    raise problem(status.HTTP_413_CONTENT_TOO_LARGE, limit.reason)
            message = await receive()
            ended = not message.get("more_body", False)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if limit is not None and received > limit.max_bytes:
                    raise problem(status.HTTP_413_CONTENT_TOO_LARGE, limit.reason)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal responding
            if message["type"] == "http.response.start" and not responding:
                responding = True
                if not await _discard(receive, unread()):
                    message = {
                        **message,
                        "headers": [
                            *message.get("headers", []),
                            (b"connection", b"close"),
                        ],
                    }
            await send(message)

        await self._app(scope, limited_receive, tracking_send)
