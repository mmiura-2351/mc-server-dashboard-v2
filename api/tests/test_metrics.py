"""Endpoint tests for GET /metrics (issue #282).

The scrape-time session factory and worker registry are overridden with fakes
(NFR-TEST-1) so no real database or gRPC fleet is touched. The endpoint must
render the Prometheus text exposition format with the key series present, label
HTTP requests by route template (not raw path), and keep serving when the DB is
down (the servers-by-state query failure is swallowed and counted).
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import (
    get_metrics_session_factory,
    get_worker_registry,
)
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry, WorkerSnapshot


class _EmptyRegistry(WorkerRegistry):
    """A registry stand-in that reports no workers (only list_workers is used)."""

    def register(self, worker):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def record_heartbeat(self, worker_id, at):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def mark_disconnected(self, worker_id, session):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def set_draining(self, worker_id, draining):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def increment_assignment(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def decrement_assignment(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def set_assignment(self, worker_id, count):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def candidates_for_placement(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def list_workers(self) -> list[WorkerSnapshot]:
        return []


class _FailingSession:
    """Async session whose query raises, simulating a DB outage at scrape."""

    async def __aenter__(self) -> "_FailingSession":
        return self

    async def __aexit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        return None

    async def execute(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("database unreachable")


def _failing_session_factory() -> _FailingSession:
    return _FailingSession()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_metrics_session_factory] = lambda: (
        _failing_session_factory
    )
    app.dependency_overrides[get_worker_registry] = lambda: _EmptyRegistry()
    with TestClient(app) as client:
        yield client


def test_metrics_renders_prometheus_text(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    # The body parses as valid Prometheus exposition.
    # The parser strips the ``_total`` suffix from counter family names.
    names = {family.name for family in text_string_to_metric_families(resp.text)}
    assert "http_requests" in names
    assert "http_request_duration_seconds" in names
    assert "servers_by_state_scrape_failures" in names


def test_metrics_db_down_still_serves_and_counts_failure(client: TestClient) -> None:
    before = _scrape_failures(client)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    after = _scrape_failures(client)
    # The DB-down scrape was swallowed (200) and the failure counter advanced.
    assert after > before


def test_metrics_labels_by_route_template_not_raw_path(client: TestClient) -> None:
    # Hit a templated route with a concrete id; the label must be the template.
    client.get("/communities/123e4567-e89b-12d3-a456-426614174000")
    resp = client.get("/metrics")
    samples = [
        sample
        for family in text_string_to_metric_families(resp.text)
        if family.name == "http_requests"
        for sample in family.samples
    ]
    routes = {sample.labels.get("route") for sample in samples}
    # The raw id never appears as a label value; the template does.
    assert not any(
        "123e4567-e89b-12d3-a456-426614174000" in (route or "") for route in routes
    )
    assert any("{community_id}" in (route or "") for route in routes)


def _scrape_failures(client: TestClient) -> float:
    resp = client.get("/metrics")
    for family in text_string_to_metric_families(resp.text):
        if family.name == "servers_by_state_scrape_failures":
            for sample in family.samples:
                if sample.name == "servers_by_state_scrape_failures_total":
                    return sample.value
    return 0.0
