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
