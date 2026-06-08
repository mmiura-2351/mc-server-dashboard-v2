"""Request-scoped correlation-ID middleware (NFR-OBS-1).

Honors an inbound ``X-Correlation-ID`` (so a request can be traced across the
reverse proxy and the Worker control plane) and otherwise mints one. The value
is bound to the logging context for the duration of the request and echoed back
on the response.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from mc_server_dashboard_api.logging import correlation_id

_HEADER = "X-Correlation-ID"


async def correlation_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    cid = request.headers.get(_HEADER) or uuid.uuid4().hex
    token = correlation_id.set(cid)
    try:
        response = await call_next(request)
    finally:
        correlation_id.reset(token)
    response.headers[_HEADER] = cid
    return response


async def strip_no_content_body_headers_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Drop the entity-body headers from ``204 No Content`` responses (issue #633).

    A handler that declares ``status_code=204`` and returns ``None`` is rendered
    by the default ``JSONResponse``, which still stamps ``Content-Type:
    application/json`` (and a ``Content-Length``) onto an empty body. A 204 must
    not advertise a representation, so strip both here — centrally, for every
    such route — rather than per handler. Deleting an absent header is a no-op,
    so this is harmless for routes that already return a bare ``Response``.
    """

    response = await call_next(request)
    if response.status_code == 204:
        del response.headers["content-type"]
        del response.headers["content-length"]
    return response
