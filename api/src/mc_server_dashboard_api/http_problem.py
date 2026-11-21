"""Central RFC 9457 ``application/problem+json`` error mechanism (issue #371).

Every application-raised error response across the HTTP surface shares one body
shape so clients (the Web UI error layer, WEBUI_SPEC.md Section 7.4) branch on
exactly one contract. A response carries ``Content-Type:
application/problem+json`` with at least ``type``, ``title`` and ``status``
(RFC 9457 Section 3.1).

The machine-readable reason codes that previously appeared as the bare
``detail`` string or the ``detail.reason`` object are unified here:

* the reason is the terminal segment of a stable ``type`` URI under the
  ``urn:mcsd:error:`` scheme — a URN, so it never fakes a resolvable domain
  (RFC 9457 permits a non-dereferenceable ``type``); and
* it is also surfaced verbatim as the ``reason`` extension member, keeping the
  code greppable in source and trivial for clients to switch on.

Extra per-error context (a field name, a conflicting attribute) rides as RFC
9457 extension members alongside ``reason``.

Routes never build error bodies by hand: they raise :func:`problem` (a
:class:`ProblemException`) and the handlers installed by
:func:`install_problem_handlers` render the body. Plain ``HTTPException`` and
FastAPI's ``RequestValidationError`` are funnelled through the same handlers so
there is no second shape. The guard test ``tests/test_error_shape_guard.py``
forbids ad-hoc ``detail=`` ``HTTPException`` usage outside this module.
"""

from __future__ import annotations

import http
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from mc_server_dashboard_api.core.adapters.database import (
    is_transient_database_error,
)

PROBLEM_CONTENT_TYPE = "application/problem+json"

_logger = logging.getLogger(__name__)

# URN scheme for the ``type`` member: ``urn:mcsd:error:<reason>``. A URN (RFC
# 8141) is a stable identifier that intentionally does not resolve, so we never
# imply a documentation domain we do not host (issue #371 PM decision).
_TYPE_PREFIX = "urn:mcsd:error:"

# Reason assigned to FastAPI/Starlette's own ``HTTPException`` responses that
# carry no application reason code (e.g. routing 404, 405). Keyed by status so
# the generic-handler output is still a stable, greppable reason.
_STATUS_REASON: dict[int, str] = {
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
}

# Reason for the unified validation-error response (request body / query / path
# validation, FastAPI ``RequestValidationError``).
VALIDATION_REASON = "validation_error"

# Reason for the catch-all 500: any exception that is NOT a ProblemException /
# HTTPException / RequestValidationError (e.g. an OSError escaping the at-rest
# write path, issue #542). Without a handler such an exception returns a bare
# ``text/plain`` 500 that escapes the RFC 9457 contract; this keeps one body shape
# while logging the traceback server-side and never leaking internals to clients.
INTERNAL_ERROR_REASON = "internal_error"

# Reason for the 503 the catch-all answers instead when the exception is the
# database being unavailable (issue #4010): a refused connect, a server that is
# full, starting up or shutting down, a connection lost mid-query. Retrying
# later is the remedy, after ``Retry-After`` seconds.
DATABASE_UNAVAILABLE_REASON = "database_unavailable"
_DATABASE_RETRY_AFTER_SECONDS = 5

# An outage fails every request until it ends, so it is logged at WARNING at
# most once in this many seconds, with no traceback; each request's traceback
# is logged at DEBUG.
_OUTAGE_WARNING_INTERVAL_SECONDS = 30.0
_last_outage_warning: float | None = None


def _title_for(status_code: int) -> str:
    """The RFC 9457 ``title`` — the HTTP status phrase for ``status_code``."""

    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def type_uri(reason: str) -> str:
    """The stable ``type`` URI for a machine-readable ``reason`` code."""

    return f"{_TYPE_PREFIX}{reason}"


class ProblemException(StarletteHTTPException):
    """An error to render as ``application/problem+json``.

    Subclasses ``HTTPException`` so existing ``raise``/``except HTTPException``
    flows and FastAPI's response-model machinery keep working; the body is
    produced by :func:`problem_exception_handler`, not from ``detail``.
    """

    def __init__(
        self,
        status_code: int,
        reason: str,
        *,
        extensions: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        # Keep ``detail`` set to the reason so log lines / default reprs stay
        # informative; the wire body comes from the handler.
        super().__init__(
            status_code=status_code,
            detail=reason,
            headers=dict(headers) if headers else None,
        )
        self.reason = reason
        self.extensions: dict[str, Any] = dict(extensions) if extensions else {}


def problem(
    status_code: int,
    reason: str,
    *,
    extensions: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> ProblemException:
    """Build a :class:`ProblemException` — the single error constructor routes use."""

    return ProblemException(
        status_code,
        reason,
        extensions=extensions,
        headers=headers,
    )


def _problem_body(
    status_code: int, reason: str, extensions: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": type_uri(reason),
        "title": _title_for(status_code),
        "status": status_code,
        # Surfaced as an extension member so the code stays greppable and
        # clients can switch on it without parsing the ``type`` URI.
        "reason": reason,
    }
    if extensions:
        body.update(extensions)
    return body


def problem_response(
    status_code: int,
    reason: str,
    *,
    extensions: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Render a problem+json :class:`JSONResponse`."""

    return JSONResponse(
        status_code=status_code,
        content=_problem_body(status_code, reason, extensions),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=dict(headers) if headers else None,
    )


async def problem_exception_handler(
    _request: Request, exc: ProblemException
) -> JSONResponse:
    return problem_response(
        exc.status_code,
        exc.reason,
        extensions=exc.extensions,
        headers=exc.headers,
    )


async def http_exception_handler(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Render a plain ``HTTPException`` (e.g. FastAPI routing 404/405) as problem+json.

    Application code raises :class:`ProblemException`; this handler only catches
    framework-raised plain exceptions, so the ``detail`` is the framework's
    status phrase, not a machine code. The reason is derived from the status.
    """

    reason = _STATUS_REASON.get(exc.status_code, _generic_reason(exc.status_code))
    return problem_response(exc.status_code, reason, headers=exc.headers)


async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render request-validation failures (422) as problem+json (issue #371).

    The per-field validation list rides as the ``errors`` extension member so
    clients deal with exactly one error shape. ``jsonable_encoder`` flattens any
    non-JSON values (e.g. ``ValueError`` context) the validators attached.

    Pydantic v2 entries also carry ``input`` (the submitted value) and ``ctx``
    (which can embed it — a ``value_error``'s ``ctx.error`` wraps the raising
    exception). For a secret-bearing field these echo the plaintext into a body
    that clients and proxies routinely log, so both are dropped unconditionally;
    clients only need ``loc``/``msg``/``type`` for field-level display
    (WEBUI_SPEC.md Section 7.4, issue #393).
    """

    return await problem_exception_handler(_request, validation_problem(exc.errors()))


def validation_problem(errors: Sequence[Any]) -> ProblemException:
    """The 422 :func:`validation_exception_handler` renders, as an exception.

    For code that has to *raise* a validation failure rather than let the
    framework's own error reach the handler: a header the data plane parses
    itself (``dataplane/api/transfers.py``).
    """

    from fastapi.encoders import jsonable_encoder

    scrubbed = [
        {key: value for key, value in error.items() if key not in ("input", "ctx")}
        for error in errors
    ]
    return problem(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        VALIDATION_REASON,
        extensions={"errors": jsonable_encoder(scrubbed)},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render any otherwise-unhandled exception as problem+json ``internal_error``.

    A route that raises something other than a :class:`ProblemException` /
    ``HTTPException`` / ``RequestValidationError`` (e.g. an ``OSError`` escaping the
    at-rest write path, issue #542) would otherwise return a bare ``text/plain``
    500 that escapes the RFC 9457 contract. This last-resort handler logs the full
    traceback server-side for diagnosis and returns a 500 whose body leaks no
    exception internals (no message, no path), keeping one error shape (#371).
    A database outage is answered 503 ``database_unavailable`` instead (#4010).
    """

    if _is_client_disconnect(exc):
        raise  # let Starlette handle normally (issue #2161)
    return _unhandled_exception_response(request, exc)


def _unhandled_exception_response(request: Request, exc: Exception) -> JSONResponse:
    """The answer to an exception no handler took, shared by both catch-alls.

    A database outage is a 503 ``database_unavailable`` (issue #4010), logged by
    :func:`_log_database_outage`. Anything else is a 500 ``internal_error`` with
    its traceback logged.
    """

    if _is_database_outage(exc):
        _log_database_outage(request, exc)
        return problem_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            DATABASE_UNAVAILABLE_REASON,
            headers={"Retry-After": str(_DATABASE_RETRY_AFTER_SECONDS)},
        )
    _logger.exception(
        "unhandled exception serving %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return problem_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR, INTERNAL_ERROR_REASON
    )


def _is_database_outage(exc: Exception) -> bool:
    """Whether ``exc`` means the database is unavailable for now (issue #4010).

    A bare ``OSError`` does not: a full disk or a permission fault escapes a
    route as one (issue #542). The app's engines raise their refused connect as
    ``DatabaseConnectError`` (``translate_connect_outages``), so the two differ.
    """

    return not isinstance(exc, OSError) and is_transient_database_error(exc)


def _log_database_outage(request: Request, exc: Exception) -> None:
    """Log an outage's request: a WARNING at most every 30 s, the traceback at DEBUG."""

    global _last_outage_warning
    now = time.monotonic()
    if (
        _last_outage_warning is None
        or now - _last_outage_warning >= _OUTAGE_WARNING_INTERVAL_SECONDS
    ):
        _last_outage_warning = now
        _logger.warning(
            "database unavailable serving %s %s (%s): answered 503; the next "
            "such requests for %g s are logged at DEBUG only",
            request.method,
            request.url.path,
            type(exc).__name__,
            _OUTAGE_WARNING_INTERVAL_SECONDS,
        )
    _logger.debug(
        "database unavailable serving %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )


def _generic_reason(status_code: int) -> str:
    """A stable snake_case reason for a status with no explicit mapping."""

    phrase = _title_for(status_code)
    return phrase.lower().replace(" ", "_").replace("-", "_") or "error"


def _is_client_disconnect(exc: Exception) -> bool:
    """Return True for exceptions that indicate a client disconnection.

    These are operational non-errors — the client hung up — and must not be
    logged at ERROR or counted as 500s (issue #2161).
    """

    if isinstance(exc, ClientDisconnect):
        return True
    # BaseHTTPMiddleware raises ``RuntimeError("No response returned.")`` when
    # the inner app produces no response, which happens on client disconnect.
    if isinstance(exc, RuntimeError) and str(exc) == "No response returned.":
        return True
    return False


async def unhandled_exception_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Catch-all middleware for unhandled exceptions (issue #1951).

    Registered as the innermost user middleware (first ``app.middleware("http")``
    call, just outside routing) so the 500 response flows outward through all
    user middleware — correlation-ID, security headers, metrics — which the
    ``add_exception_handler(Exception, ...)`` belt-and-braces path bypasses
    (Starlette attaches it to ``ServerErrorMiddleware``, outside user middleware).
    """

    try:
        return await call_next(request)
    except Exception as exc:  # noqa: BLE001 — intentional catch-all
        if _is_client_disconnect(exc):
            raise  # let Starlette handle normally (issue #2161)
        return _unhandled_exception_response(request, exc)


def install_problem_handlers(app: FastAPI) -> None:
    """Wire the problem+json handlers onto ``app`` (called from the app factory)."""

    app.add_exception_handler(ProblemException, problem_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    # Belt-and-braces: keep the exception-handler registration so an exception
    # that somehow escapes the middleware (e.g. during lifespan) still renders
    # problem+json instead of a bare text/plain 500. The middleware registered
    # in app.py (``unhandled_exception_middleware``) is the primary catch-all
    # and runs inside user middleware (issue #1951).
    app.add_exception_handler(Exception, unhandled_exception_handler)
