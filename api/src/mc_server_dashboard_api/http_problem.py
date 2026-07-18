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
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

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

    from fastapi.encoders import jsonable_encoder

    scrubbed = [
        {key: value for key, value in error.items() if key not in ("input", "ctx")}
        for error in exc.errors()
    ]
    return problem_response(
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
    """

    _logger.exception(
        "unhandled exception serving %s %s",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return problem_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR, INTERNAL_ERROR_REASON
    )


def _generic_reason(status_code: int) -> str:
    """A stable snake_case reason for a status with no explicit mapping."""

    phrase = _title_for(status_code)
    return phrase.lower().replace(" ", "_").replace("-", "_") or "error"


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
        _logger.exception(
            "unhandled exception serving %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return problem_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR, INTERNAL_ERROR_REASON
        )


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
