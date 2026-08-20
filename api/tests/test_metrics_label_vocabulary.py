"""The exposition's label values stay bounded and non-identifying (issue #2569).

``core/api/metrics.py`` claims the exposition is aggregates only. That claim is
load-bearing — it is why the endpoint is safe *by content* as well as by
reachability (issue #2565) — and until this test it was a judgement re-made by
hand on every metric addition, prompted by nothing. A metric labelled with a
``server_id``, a community name, or a raw request path would both leak
identifiers to every operator and every scrape log, and grow the emitted series
count without bound with tenant count (the classic Prometheus cardinality
explosion, which degrades a scrape rather than failing loudly). Neither shows up
in review unless the reviewer happens to know to look.

So the single test below renders the exposition from a fixture with several
servers, communities and workers present and asserts three things:

1. every metric family in the exposition is in :data:`_ALLOWED_FAMILIES`;
2. every label value is drawn from that family's declared bounded vocabulary;
3. no label value carries an identifier shape (UUID or email).

Adding a metric reddens (1) and forces its author to state the label vocabulary
in :func:`_label_vocabulary`. That friction is the point — this is a
development-time check, deliberately *not* a runtime filter that would strip or
reject labels (a code path that only ever runs once something is already wrong).

Four mechanics worth stating outright, because each one silently changes what is
being asserted:

**The registry under test is the dedicated** ``metrics.REGISTRY``, not
``prometheus_client``'s default one, so the exposition carries no ``python_*`` /
``process_*`` platform collectors and every family in it is one this repo
declares.

**The** ``_created`` **companions are folded onto their parents.**
``prometheus_client`` emits a ``<name>_created`` gauge alongside every Counter
and Histogram, carrying the parent's labels, and the text parser reports each as
a family of its own. :func:`_declared_family` folds those back so the allowlist
stays a statement about the metrics ``core.adapters.metrics`` declares. (The
fold is by name, so a *real* metric named ``<something>_created`` would be
checked against ``<something>``'s vocabulary rather than reported as unknown.)

**The four labelled metrics are cleared first.** ``metrics.REGISTRY`` is
process-wide and shared with every other test in this pytest worker — and some
of them (``tests/test_http_problem.py``) drive the middleware from their own
throwaway app, recording route templates such as ``/boom`` that exist on no real
router. Clearing means this test owns every label value in the exposition it
then asserts on, so its result does not depend on what ran before it.

**The** ``<unmatched>`` **collapse is structural, not conventional.** The
middleware labels ``route`` from ``request.scope["route"]``, and in the installed
Starlette/FastAPI only ``fastapi.routing.APIRoute.matches`` and
``APIWebSocketRoute.matches`` ever put a ``route`` key on the child scope
(``fastapi/routing.py`` lines 807 and 1001). Starlette's ``Mount.matches``
returns ``endpoint`` but no ``route``, and its plain ``Route.matches`` sets
neither — so a static-mount path, a plain Starlette route (``/api/openapi.json``)
and a 404 all leave the key absent and collapse to the literal ``<unmatched>``.
The test drives a mount path and a 404, both carrying a UUID, and pins that
neither raw path reaches the exposition.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from mc_server_dashboard_api.core.adapters import metrics
from mc_server_dashboard_api.dependencies import (
    get_metrics_session_factory,
    get_worker_registry,
)
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.registry import WorkerSnapshot
from mc_server_dashboard_api.fleet.domain.value_objects import (
    DriverKind,
    WorkerCapabilities,
    WorkerId,
)
from mc_server_dashboard_api.observability import create_observability_app
from tests.core.test_metrics_refresh import _CountSession, _Registry

# The route label a request that matched no ``APIRoute`` collapses to.
_UNMATCHED = "<unmatched>"

# Every vocabulary below is written out as a literal rather than imported from
# the code it describes: a vocabulary derived from the source widens silently
# with it, and the whole point is that widening one must be a deliberate,
# reviewable edit here.

# HTTP request methods. Bounded by the HTTP method registry, not by this repo.
_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"}
)

# HTTP status codes, as ``str(response.status_code)``.
_STATUS_CODE = re.compile(r"^[1-5][0-9]{2}$")

# The server observed states (``metrics_refresh._OBSERVED_STATES``, mirroring the
# server table's CHECK constraint — DATABASE.md Section 7).
_OBSERVED_STATES = frozenset(
    {"starting", "running", "stopping", "stopped", "restarting", "crashed", "unknown"}
)

# The Worker liveness states (``fleet.domain.entities.WorkerStatus``).
_WORKER_STATES = frozenset({"online", "draining", "offline"})

# ``le`` on the latency histogram's bucket samples: prometheus_client's default
# buckets, which ``http_request_duration_seconds`` takes unchanged.
_HISTOGRAM_BUCKETS = frozenset(
    {
        "0.005",
        "0.01",
        "0.025",
        "0.05",
        "0.075",
        "0.1",
        "0.25",
        "0.5",
        "0.75",
        "1.0",
        "2.5",
        "5.0",
        "7.5",
        "10.0",
        "+Inf",
    }
)

# Identifier shapes that must never appear in a label value, whole or embedded
# (an embedded match also covers "a path containing a UUID").
_IDENTIFIER_SHAPES = (
    ("UUID", re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")),
    ("email address", re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")),
)


def _label_vocabulary(
    route_templates: frozenset[str],
) -> dict[str, dict[str, frozenset[str] | re.Pattern[str]]]:
    """The label vocabulary of every metric the exposition is allowed to carry.

    Keyed by the family name the text parser reports, which is the declared
    metric name with the ``_total`` suffix stripped from counters. The value maps
    each label the family may carry to its permitted values — a literal set, or a
    pattern where the set is bounded but too large to write out.

    **Adding a metric to** ``core.adapters.metrics`` **means adding an entry
    here**, and an entry cannot be written without deciding what the new metric's
    label values are drawn from. A metric whose answer is "a server id" or "a
    community name" is the one this test exists to stop.
    """

    return {
        "http_requests": {
            "method": _HTTP_METHODS,
            # Only a FastAPI route template, never a raw path (see the module
            # docstring on the <unmatched> collapse).
            "route": route_templates | {_UNMATCHED},
            "status": _STATUS_CODE,
        },
        "http_request_duration_seconds": {
            "method": _HTTP_METHODS,
            "route": route_templates | {_UNMATCHED},
            # Only the ``_bucket`` samples carry ``le``; ``_sum`` / ``_count``
            # carry the family's own labels alone.
            "le": _HISTOGRAM_BUCKETS,
        },
        "servers": {"observed_state": _OBSERVED_STATES},
        "workers": {"state": _WORKER_STATES},
        "reconciler_ticks": {},
        "reconciler_last_success_timestamp_seconds": {},
        "audit_write_failures": {},
        "servers_by_state_scrape_failures": {},
    }


_ALLOWED_FAMILIES = frozenset(_label_vocabulary(frozenset()))


def _declared_family(parsed_name: str) -> str:
    """Fold a parsed family name back onto the metric that declares it."""

    base = parsed_name.removesuffix("_created")
    return base if base in _ALLOWED_FAMILIES else parsed_name


def _permits(permitted: frozenset[str] | re.Pattern[str], value: str) -> bool:
    if isinstance(permitted, re.Pattern):
        return permitted.fullmatch(value) is not None
    return value in permitted


def _worker(status: WorkerStatus) -> WorkerSnapshot:
    """A Worker whose id is a UUID, so an id-shaped label would be detectable."""

    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return WorkerSnapshot(
        id=WorkerId(str(uuid.uuid4())),
        version="1.0",
        capabilities=WorkerCapabilities(
            drivers=frozenset({DriverKind.CONTAINER}), max_servers=4
        ),
        registered_at=now,
        last_heartbeat_at=now,
        status=status,
        assigned_count=1,
    )


@pytest.fixture
def exposition_client() -> Iterator[TestClient]:
    """The observability app, backed by several servers and several workers."""

    app = create_observability_app()
    rows = [("running", 4), ("stopped", 2), ("crashed", 1)]
    workers = _Registry(
        [
            _worker(WorkerStatus.ONLINE),
            _worker(WorkerStatus.ONLINE),
            _worker(WorkerStatus.DRAINING),
            _worker(WorkerStatus.OFFLINE),
        ]
    )
    app.dependency_overrides[get_metrics_session_factory] = lambda: (
        lambda: _CountSession(rows)
    )
    app.dependency_overrides[get_worker_registry] = lambda: workers
    with TestClient(app) as client:
        yield client


def test_exposition_label_values_stay_bounded_and_non_identifying(
    shared_app: FastAPI, exposition_client: TestClient
) -> None:
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    for labelled in (
        metrics.http_requests_total,
        metrics.http_request_duration_seconds,
        metrics.servers,
        metrics.workers,
    ):
        labelled.clear()

    shared_app.dependency_overrides.clear()
    with TestClient(shared_app) as api_client:
        api_client.get("/api/healthz")
        api_client.post("/api/auth/login", json={})
        api_client.get(f"/api/communities/{community_id}")
        api_client.get(f"/api/communities/{community_id}/servers/{server_id}")
        # No route matches either of these: the first 404s, the second is served
        # by the docs-assets ``StaticFiles`` mount. Both must collapse.
        api_client.get(f"/api/no-such-route/{server_id}")
        api_client.get(f"/api/docs-assets/{server_id}.css")

    body = exposition_client.get("/metrics").text
    families = list(text_string_to_metric_families(body))
    vocabulary = _label_vocabulary(
        frozenset(
            route.path
            for route in shared_app.routes
            if isinstance(route, APIRoute | APIWebSocketRoute)
        )
    )

    exposed = {_declared_family(family.name) for family in families}
    assert exposed == _ALLOWED_FAMILIES, (
        "the exposition carries a metric this test does not know about (or has "
        "lost one): state its label vocabulary in _label_vocabulary()"
    )

    routes: set[str] = set()
    for family in families:
        declared = _declared_family(family.name)
        permitted = vocabulary[declared]
        for sample in family.samples:
            assert set(sample.labels) <= set(permitted), (
                f"{sample.name} carries undeclared labels "
                f"{sorted(set(sample.labels) - set(permitted))}: state their "
                f"vocabulary in _label_vocabulary()"
            )
            for label, value in sample.labels.items():
                # The shape check runs first: it is the net for a vocabulary
                # that is itself too permissive (``route`` is a set derived
                # from the app's router, not a literal), and "this label
                # carries a UUID" is the more useful red of the two.
                for shape, pattern in _IDENTIFIER_SHAPES:
                    assert not pattern.search(value), (
                        f"{sample.name} label {label}={value!r} contains "
                        f"a(n) {shape}: the exposition must stay non-identifying"
                    )
                assert _permits(permitted[label], value), (
                    f"{sample.name} label {label}={value!r} is outside the "
                    f"vocabulary declared for {declared}: an unbounded label "
                    f"value grows the series count without bound"
                )
        if declared == "http_requests":
            routes.update(sample.labels["route"] for sample in family.samples)

    # The six driven requests, as the ``route`` label saw them: four templates,
    # and one ``<unmatched>`` covering both the 404 and the static-mount path.
    assert routes == {
        "/api/healthz",
        "/api/auth/login",
        "/api/communities/{community_id}",
        "/api/communities/{community_id}/servers/{server_id}",
        _UNMATCHED,
    }
