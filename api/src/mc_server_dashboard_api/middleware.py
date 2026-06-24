"""HTTP middleware: correlation-ID (NFR-OBS-1) and security headers (issue #635).

Honors an inbound ``X-Correlation-ID`` (so a request can be traced across the
reverse proxy and the Worker control plane) and otherwise mints one. The value
is bound to the logging context for the duration of the request and echoed back
on the response.

The security-headers middleware stamps defence-in-depth headers (CSP,
X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy) on every
response, ``Cache-Control: no-store`` on credential-bearing endpoints, and HSTS
when the request arrived over HTTPS.
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


# -- Security headers (issue #635) --

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://cdn.modrinth.com; "
    "frame-ancestors 'none'"
)

_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"

# Paths whose responses carry ``Cache-Control: no-store`` because the body
# contains credentials (tokens) or per-user data.
_NO_STORE_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/refresh",
        "/api/users/me",
    }
)


async def security_headers_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Stamp defence-in-depth security headers on every response (issue #635)."""

    response = await call_next(request)

    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = _PERMISSIONS_POLICY

    if request.url.path in _NO_STORE_PATHS:
        response.headers["Cache-Control"] = "no-store"

    # HSTS only when the request arrived over TLS (directly or via a reverse
    # proxy advertising X-Forwarded-Proto).
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if proto == "https":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    return response
