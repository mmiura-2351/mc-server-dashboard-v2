"""Process-wide Prometheus metrics registry and primitives (issue #282).

This is infrastructure: a single place that owns the metric objects and the
exposition registry behind the ``/metrics`` endpoint. It is an *adapter* module —
the application/domain layers never import it; only the edge (the HTTP middleware,
the ``/metrics`` route) and the background seams that already exist (the
reconciler loop, the audit recorder) increment the module-level counters here.
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
refreshed by the ``/metrics`` route on each scrape; the counters and histogram
are fed by the seams as requests and ticks happen.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)

# The process-wide registry the /metrics endpoint renders. Module-level so the
# background seams (reconciler loop, audit recorder) share the same metric
# objects regardless of how many apps a test process builds.
REGISTRY = CollectorRegistry()

# HTTP request count by method, route template, and status code. The route
# template (``request.scope["route"].path``) is used, never the raw path, so the
# label set stays bounded (cardinality — issue #282).
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests by method, route template, and status code.",
    ["method", "route", "status"],
    registry=REGISTRY,
)

# HTTP request latency by method and route template. Prometheus default buckets
# are appropriate for a web API (issue #282).
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds by method and route template.",
    ["method", "route"],
    registry=REGISTRY,
)

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
