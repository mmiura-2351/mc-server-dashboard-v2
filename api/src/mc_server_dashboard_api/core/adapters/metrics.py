"""Process-wide Prometheus metrics registry and primitives (issue #282).

This is infrastructure: a single place that owns the metric objects and the
exposition registry behind the ``/metrics`` endpoint. It is an *adapter* module —
the application/domain layers never import it; only the edge (the app factory,
the ``/metrics`` route) and the background seams that already exist (the
reconciler loop, the audit recorder) touch the objects here.
The import-linter contracts forbid domain/application importing ``adapters``, so
placing the counters here keeps the seams' coupling honest: a loop or recorder
adapter importing another adapter is allowed.

A dedicated :class:`CollectorRegistry` (not the global default) is used so the
process owns one explicit registry, repeated ``create_app()`` calls in tests do
not collide on the default registry, and the exposition output is deterministic.

The metric set is the M2 floor (issue #282): HTTP request count + latency by
route template and status, servers by observed state (counted at scrape time
through a bounded query), workers by state, the reconciler tick + last-success
signals, and the audit-write failure counter. The servers / workers gauges are
refreshed by the ``/metrics`` route on each scrape; the counters are fed by the
seams as ticks happen.

The HTTP request metrics are not declared here (#3563). FastAPI records them
itself through its native OpenTelemetry telemetry, under the OpenTelemetry
semantic-convention names (``http_server_request_duration_seconds``,
``http_server_active_requests``). This module owns the meter provider that
telemetry records into and the Prometheus reader that renders it into
:data:`REGISTRY`, so one scrape still returns every metric. Both are
process-wide for the same reason as the registry: a reader registers a collector
on construction, so a reader per app would duplicate the HTTP families in the
exposition.
"""

from __future__ import annotations

from fastapi.telemetry import TelemetryConfig
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
)

# The process-wide registry the /metrics endpoint renders. Module-level so the
# background seams (reconciler loop, audit recorder) share the same metric
# objects regardless of how many apps a test process builds.
REGISTRY = CollectorRegistry()

# The meter provider FastAPI's native HTTP telemetry records into, read into
# REGISTRY at scrape time. ``target_info`` and the ``otel_scope_*`` labels are
# off: the first carries the SDK's resource attributes (a fixed set of SDK
# facts, or whatever ``OTEL_RESOURCE_ATTRIBUTES`` says), the second the FastAPI
# version, and neither describes a request.
_HTTP_READER = PrometheusMetricReader(
    disable_target_info=True, scope_info_enabled=False, registry=REGISTRY
)
_METER_PROVIDER = MeterProvider(metric_readers=[_HTTP_READER])

# The ``telemetry`` argument for the public API app: HTTP request metrics into
# the provider above, and nothing else. Every key is explicit rather than left
# to FastAPI's defaults, which trace and log through the global OpenTelemetry
# providers and, when ``FASTAPI_OTEL_AUTO_CONFIGURE=true`` is set, add OTLP
# exporters from ``OTEL_EXPORTER_OTLP_*`` at startup. This deployment exports
# nothing over OTLP, so no environment variable may turn that on.
HTTP_TELEMETRY: TelemetryConfig = {
    "meter_provider": _METER_PROVIDER,
    "metrics": True,
    "tracing": False,
    "logs": False,
    "auto_configure": False,
}

# Reconciler tick counter and last-success timestamp (issue #101 seam). The loop
# increments ``reconciler_ticks_total`` each iteration it ticks and sets
# ``reconciler_last_success_timestamp_seconds`` on a clean tick, so an operator
# can alert on a stalled reconciler (now - last_success too large).
reconciler_ticks_total = Counter(
    "reconciler_ticks_total",
    "Total divergence-reconciler ticks attempted.",
    registry=REGISTRY,
)
reconciler_last_success_timestamp_seconds = Gauge(
    "reconciler_last_success_timestamp_seconds",
    "Unix timestamp of the last successful reconciler tick.",
    registry=REGISTRY,
)

# Audit-write failures (FR-AUD-2 swallow path). Incremented by the must-not-raise
# recorder when a write is swallowed, so a silently failing audit trail is
# observable instead of invisible.
audit_write_failures_total = Counter(
    "audit_write_failures_total",
    "Total audit-log writes that failed and were swallowed.",
    registry=REGISTRY,
)

# Servers by observed state, refreshed at scrape time from a bounded GROUP BY
# query (the /metrics route runs it). Every observed state is always emitted so a
# state with zero servers reports 0 rather than vanishing from the series.
servers = Gauge(
    "servers",
    "Servers by observed state.",
    ["observed_state"],
    registry=REGISTRY,
)

# Workers by state (online / draining / offline), refreshed at scrape time from
# the in-memory registry.
workers = Gauge(
    "workers",
    "Connected Workers by state.",
    ["state"],
    registry=REGISTRY,
)

# Scrape-time failures of the servers-by-state query. When the database is
# unreachable at scrape the route leaves the ``servers`` gauge untouched and bumps
# this counter, so /metrics still renders (it never 500s on a DB outage) and the
# failure is itself observable.
servers_by_state_scrape_failures_total = Counter(
    "servers_by_state_scrape_failures_total",
    "Total scrapes where the servers-by-state query failed (DB unreachable).",
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    """Render the registry to the Prometheus text exposition format.

    Returns the body bytes and the content type the /metrics route sets.
    """

    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
