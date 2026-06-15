"""Benchmark: health-check endpoint round-trip (issue #1122).

Measures the full FastAPI request/response cycle for the lightest read
endpoint.  The database Port is faked so only framework overhead is measured.

To add a new endpoint benchmark, copy this file, swap the route and the
dependency override, and name the function ``test_bench_<your_endpoint>``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.core.domain.health import DatabasePing
from mc_server_dashboard_api.dependencies import get_database_ping


class _FakePing(DatabasePing):
    async def is_reachable(self) -> bool:
        return True


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_database_ping] = _FakePing
    with TestClient(app) as c:
        yield c


def test_bench_healthz(benchmark: Any, client: TestClient) -> None:
    """Round-trip time for GET /api/healthz."""
    result = benchmark(client.get, "/api/healthz")
    assert result.status_code == 200
