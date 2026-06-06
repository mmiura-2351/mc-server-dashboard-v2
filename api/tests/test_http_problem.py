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

from mc_server_dashboard_api.http_problem import (
    ProblemException,
    install_problem_handlers,
    problem,
)


def _client() -> TestClient:
    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/bare")
    def bare() -> None:
        raise problem(status.HTTP_404_NOT_FOUND, "not_found")

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
