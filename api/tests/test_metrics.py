"""Endpoint tests for the Prometheus exposition (issues #282, #2565).

The exposition is served by the **observability app** on its own listener, not
by the public API app: `/api/metrics` was reachable from the internet because
the Cloudflare tunnel forwards the whole hostname to `api:8000` (issue #2565).

The scrape-time session factory and worker registry are overridden with fakes
(NFR-TEST-1) so no real database or gRPC fleet is touched. The endpoint must
render the Prometheus text exposition format with the key series present, label
HTTP requests by route template (not raw path), and keep serving when the DB is
down (the servers-by-state query failure is swallowed and counted).

The HTTP request metrics are FastAPI's native OpenTelemetry ones (issue #3563),
under the semantic-convention names: ``http_server_request_duration_seconds``
(whose ``_count`` is the request count) labelled ``http_route``,
``http_request_method`` and ``http_response_status_code``. FastAPI reads its
OpenTelemetry settings from the environment unless told otherwise, so the
last test here pins that no environment variable can start an OTLP export.
"""

import logging
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import metrics, trace
from prometheus_client.parser import text_string_to_metric_families

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import (
    get_metrics_session_factory,
    get_worker_registry,
)
from mc_server_dashboard_api.fleet.domain.registry import WorkerRegistry, WorkerSnapshot
from mc_server_dashboard_api.observability import create_observability_app


class _EmptyRegistry(WorkerRegistry):
    """A registry stand-in that reports no workers (only list_workers is used)."""

    def register(self, worker, held_servers=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def record_held_generation(  # type: ignore[no-untyped-def]
        self, worker_id, server_id, generation
    ):
        raise NotImplementedError

    def held_generation(self, worker_id, server_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def record_heartbeat(self, worker_id, session, at):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def mark_disconnected(self, worker_id, session):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def is_current_session(self, worker_id, session):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def set_draining(self, worker_id, draining):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def reserve(self, worker_id, server_id, memory_mb):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def reserved_memory_mb(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def committed_memory_mb(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def assignment_epoch(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def release_reservation(self, worker_id, server_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def increment_assignment(self, worker_id, server_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def decrement_assignment(self, worker_id, server_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def set_assignment(  # type: ignore[no-untyped-def]
        self, worker_id, session, assignments, snapshot_epoch
    ):
        raise NotImplementedError

    def candidates_for_placement(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def list_workers(self) -> list[WorkerSnapshot]:
        return []

    def get(self, worker_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError


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
    """A client for the observability app, with the scrape seams faked."""

    app = create_observability_app()
    app.dependency_overrides[get_metrics_session_factory] = lambda: (
        _failing_session_factory
    )
    app.dependency_overrides[get_worker_registry] = lambda: _EmptyRegistry()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def api_client(shared_app: FastAPI) -> Iterator[TestClient]:
    """A client for the public API app — the one the tunnel forwards to."""

    shared_app.dependency_overrides.clear()
    with TestClient(shared_app) as client:
        yield client


def test_metrics_absent_from_the_public_api_app(api_client: TestClient) -> None:
    # The exposition must not be reachable on the public API port at all: the
    # Cloudflare tunnel forwards the whole hostname to api:8000 (issue #2565).
    assert api_client.get("/api/metrics").status_code == 404


def test_metrics_renders_prometheus_text(
    api_client: TestClient, client: TestClient
) -> None:
    # The HTTP families appear once a request has been recorded.
    api_client.get("/api/healthz")
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    # The body parses as valid Prometheus exposition.
    # The parser strips the ``_total`` suffix from counter family names.
    names = {family.name for family in text_string_to_metric_families(resp.text)}
    assert "http_server_request_duration_seconds" in names
    assert "http_server_active_requests" in names
    assert "servers_by_state_scrape_failures" in names


def test_metrics_db_down_still_serves_and_counts_failure(client: TestClient) -> None:
    before = _scrape_failures(client)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    after = _scrape_failures(client)
    # The DB-down scrape was swallowed (200) and the failure counter advanced.
    assert after > before


def test_metrics_labels_by_full_route_template_not_raw_path(
    api_client: TestClient, client: TestClient
) -> None:
    # Hit a templated route with a concrete id; the label must be the template,
    # with the ``/api`` prefix of the parent router included (FastAPI 0.137+
    # dropped include prefixes from ``scope["route"]``, issue #3470). The API app
    # serves the request; the series live in the process-wide registry that the
    # observability app renders.
    community_id = "123e4567-e89b-12d3-a456-426614174000"
    before = _request_counts(client)
    api_client.get(f"/api/communities/{community_id}")
    recorded = _new_requests(before, _request_counts(client))
    assert {labels["http_route"] for labels in recorded} == {
        "/api/communities/{community_id}"
    }
    assert not any(community_id in value for value in _all_label_values(client))


def test_a_scrape_does_not_count_itself(client: TestClient) -> None:
    # As before the move to FastAPI telemetry: the observability app records no
    # HTTP metrics, so scraping it adds nothing to the request series.
    before = _request_counts(client)
    client.get("/metrics")
    assert _new_requests(before, _request_counts(client)) == []
    assert "/metrics" not in _all_label_values(client)


@pytest.mark.parametrize(
    "build",
    [create_app, create_observability_app],
    ids=["public-app", "observability-app"],
)
def test_environment_cannot_start_otlp_export(
    build: Callable[[], FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``FASTAPI_OTEL_AUTO_CONFIGURE=true`` must not add an exporter (#3563).

    FastAPI's telemetry defaults to reading that variable, and, when it is set,
    to adding OTLP exporters for every ``OTEL_EXPORTER_OTLP_*`` endpoint at
    lifespan startup and installing global providers for them. The OTLP exporter
    is not installed here, so an attempt surfaces as FastAPI's "automatic
    telemetry configuration failed" warning rather than as traffic. Both apps
    pass ``auto_configure: False`` explicitly, so neither attempts it: no
    warning, and the global providers are untouched.
    """

    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    tracer_provider = trace.get_tracer_provider()
    meter_provider = metrics.get_meter_provider()
    app = build()
    with caplog.at_level(logging.WARNING, logger="fastapi"), TestClient(app):
        pass
    assert not [
        record for record in caplog.records if "telemetry" in record.getMessage()
    ]
    assert trace.get_tracer_provider() is tracer_provider
    assert metrics.get_meter_provider() is meter_provider


_LabelSet = frozenset[tuple[str, str]]


def _request_counts(client: TestClient) -> dict[_LabelSet, float]:
    """Every ``http_server_request_duration_seconds_count`` series, by label set."""

    resp = client.get("/metrics")
    return {
        frozenset(sample.labels.items()): sample.value
        for family in text_string_to_metric_families(resp.text)
        if family.name == "http_server_request_duration_seconds"
        for sample in family.samples
        if sample.name == "http_server_request_duration_seconds_count"
    }


def _new_requests(
    before: dict[_LabelSet, float], after: dict[_LabelSet, float]
) -> list[dict[str, str]]:
    """The label sets whose request count moved between two scrapes."""

    return [
        dict(labels)
        for labels, value in after.items()
        if value != before.get(labels, 0.0)
    ]


def _all_label_values(client: TestClient) -> set[str]:
    resp = client.get("/metrics")
    return {
        value
        for family in text_string_to_metric_families(resp.text)
        for sample in family.samples
        for value in sample.labels.values()
    }


def _scrape_failures(client: TestClient) -> float:
    resp = client.get("/metrics")
    for family in text_string_to_metric_families(resp.text):
        if family.name == "servers_by_state_scrape_failures":
            for sample in family.samples:
                if sample.name == "servers_by_state_scrape_failures_total":
                    return sample.value
    return 0.0
