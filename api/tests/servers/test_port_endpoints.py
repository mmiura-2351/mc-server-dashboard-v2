"""Endpoint tests for the ports router (issue #243).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the read
use cases faked (no database). Verifies authentication (any logged-in user, no
community scoping), the happy paths, and the count-bound edge (422 on a bad
count).
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import (
    get_check_port,
    get_current_user,
    get_list_available_ports,
)
from tests.identity.fakes import make_user


class _FakeUseCase:
    def __init__(self, *, result: object) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self._result


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(
    *,
    authenticated: bool = True,
    check: _FakeUseCase | None = None,
    available: _FakeUseCase | None = None,
) -> object:
    app = create_app()
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: make_user()
    if check is not None:
        app.dependency_overrides[get_check_port] = lambda: check
    if available is not None:
        app.dependency_overrides[get_list_available_ports] = lambda: available
    return app


def test_check_requires_authentication() -> None:
    app = _app(authenticated=False)
    client = next(_client(app))
    resp = client.get("/api/ports/check/25565")
    assert resp.status_code == 401


def test_check_returns_availability() -> None:
    check = _FakeUseCase(result={"port": 25565, "in_range": True, "available": False})
    app = _app(check=check)
    client = next(_client(app))
    resp = client.get("/api/ports/check/25565")
    assert resp.status_code == 200
    assert resp.json() == {"port": 25565, "in_range": True, "available": False}
    assert check.calls == [{"port": 25565}]


def test_available_requires_authentication() -> None:
    app = _app(authenticated=False)
    client = next(_client(app))
    resp = client.get("/api/ports/available")
    assert resp.status_code == 401


def test_available_defaults_count_to_one() -> None:
    available = _FakeUseCase(result=[25565])
    app = _app(available=available)
    client = next(_client(app))
    resp = client.get("/api/ports/available")
    assert resp.status_code == 200
    assert resp.json() == {"ports": [25565]}
    assert available.calls == [{"count": 1}]


def test_available_honors_count() -> None:
    available = _FakeUseCase(result=[25565, 25566, 25567])
    app = _app(available=available)
    client = next(_client(app))
    resp = client.get("/api/ports/available?count=3")
    assert resp.status_code == 200
    assert resp.json() == {"ports": [25565, 25566, 25567]}
    assert available.calls == [{"count": 3}]


def test_available_rejects_zero_count() -> None:
    available = _FakeUseCase(result=[])
    app = _app(available=available)
    client = next(_client(app))
    resp = client.get("/api/ports/available?count=0")
    assert resp.status_code == 422
    assert available.calls == []


def test_available_rejects_over_cap_count() -> None:
    available = _FakeUseCase(result=[])
    app = _app(available=available)
    client = next(_client(app))
    resp = client.get("/api/ports/available?count=101")
    assert resp.status_code == 422
    assert available.calls == []
