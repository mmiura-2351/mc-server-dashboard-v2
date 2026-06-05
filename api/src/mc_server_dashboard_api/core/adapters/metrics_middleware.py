"""HTTP request metrics middleware (issue #282).

Records request count and latency into the Prometheus metrics behind /metrics.
The labels use the matched *route template*
(``request.scope["route"].path`` — e.g. ``/servers/{server_id}``), never the raw
path, so the label cardinality stays bounded no matter how many distinct ids are
requested. The route object is only on the scope after routing has run, so it is
read after ``call_next``; a request that matched no route (a 404) is labelled with
a single ``<unmatched>`` template rather than its raw path, again to bound
cardinality.

This is an adapter at the edge: it imports the metrics primitives module directly,
which the wiring layer (app factory) installs as middleware.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response

from mc_server_dashboard_api.core.adapters.metrics import (
    http_request_duration_seconds,
    http_requests_total,
)

_UNMATCHED = "<unmatched>"


async def metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start
    route = request.scope.get("route")
    template = getattr(route, "path", None) or _UNMATCHED
    method = request.method
    http_requests_total.labels(
        method=method, route=template, status=str(response.status_code)
    ).inc()
    http_request_duration_seconds.labels(method=method, route=template).observe(elapsed)
    return response
