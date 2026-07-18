"""Unit tests for the central RFC 9457 problem+json error mechanism.

Every application-raised error response uses ``application/problem+json`` with
at least ``type``, ``title``, ``status`` (issue #371). The machine-readable
reason code is the terminal segment of a stable ``urn:mcsd:error:<reason>``
``type`` URI and is also surfaced as the ``reason`` extension member so clients
and tests can branch on one stable shape.
"""

from __future__ import annotations

from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from mc_server_dashboard_api.core.adapters.metrics import http_requests_total
from mc_server_dashboard_api.core.adapters.metrics_middleware import metrics_middleware
from mc_server_dashboard_api.http_problem import (
    ProblemException,
    install_problem_handlers,
    problem,
    unhandled_exception_middleware,
)
from mc_server_dashboard_api.middleware import (
    correlation_id_middleware,
    security_headers_middleware,
)


class _Secret(BaseModel):
    password: str = Field(min_length=1, max_length=8)


def _client() -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/bare")
    def bare() -> None:
        raise problem(status.HTTP_404_NOT_FOUND, "not_found")

    @app.post("/secret")
    def secret(body: _Secret) -> None:  # noqa: ARG001 — body drives a 422
        return None

    @app.get("/with-extensions")
    def with_extensions() -> None:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "too_short",
            extensions={"field": "password"},
        )

    @app.get("/with-headers")
    def with_headers() -> None:
        raise problem(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/validated")
    def validated(limit: int) -> int:  # noqa: ARG001 — query param drives 422
        return 0

    @app.get("/boom")
    def boom() -> None:
        # An unexpected OSError (e.g. the IsADirectoryError the at-rest write
        # raised, issue #542) must be normalized to problem+json, not leak as a
        # bare text/plain 500 with the secret in the detail.
        raise IsADirectoryError(21, "Is a directory: '/secret/path'")

    return TestClient(app, raise_server_exceptions=False)


def test_problem_returns_problem_exception() -> None:
    exc = problem(status.HTTP_404_NOT_FOUND, "not_found")
    assert isinstance(exc, ProblemException)
    assert exc.reason == "not_found"
    assert exc.status_code == status.HTTP_404_NOT_FOUND


def test_application_error_renders_problem_json() -> None:
    resp = _client().get("/bare")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["type"] == "urn:mcsd:error:not_found"
    assert body["status"] == 404
    assert body["reason"] == "not_found"
    assert isinstance(body["title"], str) and body["title"]


def test_extension_members_are_top_level() -> None:
    body = _client().get("/with-extensions").json()
    assert body["type"] == "urn:mcsd:error:too_short"
    assert body["reason"] == "too_short"
    assert body["field"] == "password"


def test_headers_are_preserved() -> None:
    resp = _client().get("/with-headers")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json()["reason"] == "invalid_credentials"


def test_plain_http_exception_is_rendered_as_problem_json() -> None:
    # FastAPI's own routing 404 (unknown path) goes through the bare
    # HTTPException handler, which must also emit problem+json.
    resp = _client().get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["status"] == 404
    assert body["type"].startswith("urn:mcsd:error:")


def test_request_validation_error_is_problem_json() -> None:
    resp = _client().get("/validated")  # missing required ?limit=
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["type"] == "urn:mcsd:error:validation_error"
    assert body["reason"] == "validation_error"
    assert body["status"] == 422
    assert isinstance(body["errors"], list) and body["errors"]


def test_unexpected_exception_is_problem_json_not_bare_500() -> None:
    # An unexpected OSError escaping a route must be normalized to problem+json
    # ``internal_error`` (500), not a bare text/plain 500 that escapes RFC 9457
    # (issue #542, consistent with #371).
    resp = _client().get("/boom")
    assert resp.status_code == 500
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["type"] == "urn:mcsd:error:internal_error"
    assert body["reason"] == "internal_error"
    assert body["status"] == 500
    # The body must not leak the exception's internals (path / errno message).
    assert "secret" not in resp.text
    assert "Is a directory" not in resp.text


def test_validation_error_entries_omit_input_and_ctx() -> None:
    # A structural 422 on a secret-bearing field must not echo the submitted
    # value: ``input`` (the raw value) and ``ctx`` (can embed it, e.g. a
    # ``value_error``'s wrapped exception) are dropped from every entry, while
    # clients keep ``loc``/``msg``/``type`` for field-level display (#393).
    secret = "supersecretpassword"  # 19 chars > max_length=8
    resp = _client().post("/secret", json={"password": secret})
    assert resp.status_code == 422
    assert secret not in resp.text
    entry = resp.json()["errors"][0]
    assert "input" not in entry
    assert "ctx" not in entry
    assert entry["loc"] == ["body", "password"]
    assert entry["type"] == "string_too_long"
    assert isinstance(entry["msg"], str) and entry["msg"]


# -- Regression tests: unhandled 500 must flow through user middleware (#1951) --


def _client_with_middleware() -> TestClient:
    """Build a test app with the full user middleware stack.

    Mirrors the ``app.py`` registration order so the 500 response produced by
    ``unhandled_exception_middleware`` flows through correlation-ID, security
    headers, and metrics middleware — the exact path that was broken before
    issue #1951.
    """

    app = FastAPI()
    install_problem_handlers(app)
    # Same registration order as app.py: innermost first.
    app.middleware("http")(unhandled_exception_middleware)
    app.middleware("http")(correlation_id_middleware)
    app.middleware("http")(security_headers_middleware)
    app.middleware("http")(metrics_middleware)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False)


def test_unhandled_500_carries_security_headers() -> None:
    """An unhandled-exception 500 must carry the defence-in-depth security
    headers (issue #1951). Before the fix, ``add_exception_handler(Exception)``
    ran outside all user middleware, so the 500 had no security headers."""

    resp = _client_with_middleware().get("/boom")
    assert resp.status_code == 500
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["Permissions-Policy"]


def test_unhandled_500_carries_correlation_id() -> None:
    """An unhandled-exception 500 must echo back the correlation ID
    (issue #1951). Before the fix, the 500 bypassed the correlation-ID
    middleware so no X-Correlation-ID header was set."""

    client = _client_with_middleware()
    # With an inbound correlation ID.
    resp = client.get("/boom", headers={"X-Correlation-ID": "trace-abc"})
    assert resp.status_code == 500
    assert resp.headers["X-Correlation-ID"] == "trace-abc"
    # Without an inbound correlation ID — one must still be minted.
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert resp.headers.get("X-Correlation-ID")


def test_unhandled_500_increments_metrics() -> None:
    """An unhandled-exception 500 must be visible to ``http_requests_total``
    (issue #1951). Before the fix, the 500 bypassed the metrics middleware
    so it was invisible to the counter."""

    client = _client_with_middleware()
    # Snapshot the counter before the request. The label combination for
    # an unmatched-template GET 500 is method=GET, route=<unmatched> or
    # the actual template, status=500. We check any 500 increment.
    before = http_requests_total.labels(
        method="GET", route="/boom", status="500"
    )._value.get()
    client.get("/boom")
    after = http_requests_total.labels(
        method="GET", route="/boom", status="500"
    )._value.get()
    assert after == before + 1
