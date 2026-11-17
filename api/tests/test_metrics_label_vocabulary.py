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

So the main test below renders the exposition from a fixture with several
servers, communities and workers present and asserts three things:

1. every metric family in the exposition is in :data:`_ALLOWED_FAMILIES`;
2. every label value is drawn from that family's declared bounded vocabulary;
3. no label value carries an identifier shape (UUID or email).

Adding a metric reddens (1) and forces its author to state the label vocabulary
in :func:`_label_vocabulary`. That friction is the point — this is a
development-time check, deliberately *not* a runtime filter that would strip or
reject labels (a code path that only ever runs once something is already wrong).

Mechanics worth stating outright, because each one silently changes what is
being asserted:

**The registry under test is the dedicated** ``metrics.REGISTRY``, not
``prometheus_client``'s default one, so the exposition carries no ``python_*`` /
``process_*`` platform collectors and every family in it is one this repo
declares, or one FastAPI's telemetry records into the meter provider that
``core.adapters.metrics`` owns (issue #3563).

**The** ``_created`` **companions are folded onto their parents.**
``prometheus_client`` emits a ``<name>_created`` gauge alongside every Counter
and Histogram, carrying the parent's labels, and the text parser reports each as
a family of its own. :func:`_declared_family` folds those back so the allowlist
stays a statement about the metrics the exposition declares. (The fold is by
name, so a *real* metric named ``<something>_created`` would be checked against
``<something>``'s vocabulary rather than reported as unknown.)

**The HTTP series cannot be cleared, so the vocabulary is checked over all of
them and the driven requests over a delta.** The OpenTelemetry instruments have
no public reset, and the meter provider is process-wide, so the exposition also
carries whatever earlier tests in this pytest worker recorded. Every one of
those series must still be inside the vocabulary — that is a stronger check, not
a weaker one. The exact ``http_route`` / ``http_request_method`` sets the driven
requests produce are asserted on the series whose request count moved.

**An absent label renders as** ``""``. The Prometheus exporter backfills every
label a family carries anywhere onto every series of it, so ``http_route`` is
``""`` on a request that matched no route, and ``error_type`` is ``""`` on a
request that did not fail. Prometheus itself treats an empty label as absent.

**A request that matched no route carries no route.** FastAPI records
``http_route`` only when routing selects a route (including the 405 partial
match and the trailing-slash redirect) or a mount; a 404 that matched nothing
has none, so its raw path never reaches a label. A mount is labelled by its
template, ``<prefix>/{path}`` (the docs-assets ``StaticFiles``; the SPA mount at
``/{path}`` when a dist dir is configured), never by the file path requested.
The test drives a 404 and a mount path, both carrying a UUID, and pins that
neither raw path reaches the exposition.

**The** ``http_request_method`` **label is bounded by FastAPI, not by the
request** (issue #2762). ``uvicorn`` runs on ``h11`` here, whose grammar accepts
any RFC token as a method, and an invented token on a templated path takes the
405 partial-match branch, which still selects a route — so an unbounded method
label would mint a fresh series per token, from an unauthenticated client.
FastAPI labels any method outside the OpenTelemetry known-method set as
``_OTHER``. The test drives one such request and pins that it is labelled
``_OTHER``.

That set is FastAPI's, not this repo's, and nothing in FastAPI's public API
exposes it, so :data:`_KNOWN_METHODS` restates it as a literal and two tests
keep the literal honest from both ends (issues #2872, #2931):
:func:`test_every_known_method_is_labelled_as_itself` drives each method in it
and pins that FastAPI labels it as itself, not ``_OTHER``; and
:func:`test_every_route_method_is_a_known_method` walks the app's routes and
pins that every method they serve is in it, so a route can never be served and
then silently labelled ``_OTHER``. (``OTEL_INSTRUMENTATION_HTTP_KNOWN_METHODS``
replaces the set at runtime; this deployment does not set it.)
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from starlette.routing import BaseRoute, Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles

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
from mc_server_dashboard_api.http_body_limit import ANONYMOUS_MAX_REQUEST_BODY_BYTES
from mc_server_dashboard_api.observability import create_observability_app
from tests.core.test_metrics_refresh import _CountSession, _Registry

# The method label a request outside FastAPI's known-method set collapses to.
_OTHER_METHOD = "_OTHER"

# Every vocabulary below is written out as a literal rather than imported from
# the code it describes: a vocabulary derived from the source widens silently
# with it, and the whole point is that widening one must be a deliberate,
# reviewable edit here.

# The HTTP methods FastAPI's telemetry labels as themselves: the OpenTelemetry
# semantic conventions' known methods. Anything else is ``_OTHER``. Reconciled
# against FastAPI's behaviour by ``test_every_known_method_is_labelled_as_itself``.
_KNOWN_METHODS = frozenset(
    {
        "CONNECT",
        "DELETE",
        "GET",
        "HEAD",
        "OPTIONS",
        "PATCH",
        "POST",
        "PUT",
        "QUERY",
        "TRACE",
    }
)
_HTTP_METHODS = _KNOWN_METHODS | {_OTHER_METHOD}

# HTTP status codes, as ``str(status_code)``.
_STATUS_CODE = re.compile(r"^[1-5][0-9]{2}$")

# ``error_type`` on a failed request: the status code of a 5xx answered without
# an exception reaching FastAPI, or the qualified class name of the exception
# that did. Both bounded by the code, never by the request. ``""`` on success.
_ERROR_TYPE = re.compile(r"^(|[1-5][0-9]{2}|[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_]\w*)*)$")

# The server observed states (``metrics_refresh._OBSERVED_STATES``, mirroring the
# server table's CHECK constraint — DATABASE.md Section 7).
_OBSERVED_STATES = frozenset(
    {"starting", "running", "stopping", "stopped", "restarting", "crashed", "unknown"}
)

# The Worker liveness states (``fleet.domain.entities.WorkerStatus``).
_WORKER_STATES = frozenset({"online", "draining", "offline"})

# ``le`` on the latency histogram's bucket samples: the boundaries FastAPI
# advises for ``http.server.request.duration``, the semantic conventions' ones.
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

# ``url.scheme`` and ``network.protocol.version``, as the ASGI server reports
# them for an HTTP request.
_URL_SCHEMES = frozenset({"http", "https"})
_PROTOCOL_VERSIONS = frozenset({"1.0", "1.1", "2", "3"})

# The SPA mount's template (``webui.py`` mounts ``/``). The shared app has no
# dist dir, so its route walk does not see it, but other tests in this worker
# build an app with one and record it.
_SPA_MOUNT = "/{path}"

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
        "http_server_request_duration_seconds": {
            "http_request_method": _HTTP_METHODS,
            # Only a route or mount template, never a raw path; ``""`` when no
            # route matched (see the module docstring).
            "http_route": route_templates | {_SPA_MOUNT, ""},
            "http_response_status_code": _STATUS_CODE,
            "error_type": _ERROR_TYPE,
            "url_scheme": _URL_SCHEMES,
            "network_protocol_version": _PROTOCOL_VERSIONS,
            # Only the ``_bucket`` samples carry ``le``; ``_sum`` / ``_count``
            # carry the family's own labels alone.
            "le": _HISTOGRAM_BUCKETS,
        },
        "http_server_active_requests": {
            "http_request_method": _HTTP_METHODS,
            "url_scheme": _URL_SCHEMES,
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


def _route_templates(app: FastAPI) -> frozenset[str]:
    """Every route and mount template routing can select, ``/api`` included."""

    return frozenset(
        context.path_format
        for context in iter_route_contexts(app.routes)
        if context.path_format is not None
    )


_LabelSet = frozenset[tuple[str, str]]


def _request_counts(client: TestClient) -> dict[_LabelSet, float]:
    """Every ``http_server_request_duration_seconds_count`` series, by label set."""

    return {
        frozenset(sample.labels.items()): sample.value
        for family in text_string_to_metric_families(client.get("/metrics").text)
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


@pytest.fixture
def api_client(shared_app: FastAPI) -> Iterator[TestClient]:
    """A client for the public API app, which records the HTTP metrics."""

    shared_app.dependency_overrides.clear()
    with TestClient(shared_app) as client:
        yield client


def test_exposition_label_values_stay_bounded_and_non_identifying(
    shared_app: FastAPI, api_client: TestClient, exposition_client: TestClient
) -> None:
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()

    before = _request_counts(exposition_client)
    api_client.get("/api/healthz")
    api_client.post("/api/auth/login", json={})
    # Refused by the body limit before a byte is read: the route is already
    # selected, so the 413 is labelled by its template like any other answer.
    api_client.post(
        "/api/auth/login",
        content=b"",
        headers={"Content-Length": str(ANONYMOUS_MAX_REQUEST_BODY_BYTES + 1)},
    )
    api_client.get(f"/api/communities/{community_id}")
    api_client.get(f"/api/communities/{community_id}/servers/{server_id}")
    # The trailing-slash redirect is labelled by the route it redirects to.
    api_client.get("/api/healthz/", follow_redirects=False)
    # No route matches this one: a 404 with no ``http_route`` at all.
    api_client.get(f"/api/no-such-route/{server_id}")
    # Served (and 404'd) by the docs-assets ``StaticFiles`` mount: labelled by
    # the mount's template, not the requested file.
    api_client.get(f"/api/docs-assets/{server_id}.css")
    # An invented method token, which h11 accepts on the wire and the 405
    # partial match still resolves to a route template. Must collapse.
    api_client.request("BREW", "/api/healthz")
    recorded = _new_requests(before, _request_counts(exposition_client))

    body = exposition_client.get("/metrics").text
    families = list(text_string_to_metric_families(body))
    vocabulary = _label_vocabulary(_route_templates(shared_app))

    exposed = {_declared_family(family.name) for family in families}
    assert exposed == _ALLOWED_FAMILIES, (
        "the exposition carries a metric this test does not know about (or has "
        "lost one): state its label vocabulary in _label_vocabulary()"
    )

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
                # that is itself too permissive (``http_route`` is a set
                # derived from the app's router, not a literal), and "this
                # label carries a UUID" is the more useful red of the two.
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

    # The driven requests, as the ``http_route`` label saw them: the full
    # templates (``/api`` prefix included), the mount's template, and ``""``
    # for the 404 that matched nothing.
    assert {labels["http_route"] for labels in recorded} == {
        "/api/healthz",
        "/api/auth/login",
        "/api/communities/{community_id}",
        "/api/communities/{community_id}/servers/{server_id}",
        "/api/docs-assets/{path}",
        "",
    }
    assert {
        labels["http_response_status_code"]
        for labels in recorded
        if labels["http_route"] == ""
    } == {"404"}
    # And as the ``http_request_method`` label saw them. ``BREW`` reached a real
    # route template (the 405 partial match) and still contributed no series of
    # its own: FastAPI bounds the label, the request does not.
    assert {labels["http_request_method"] for labels in recorded} == {
        "GET",
        "POST",
        _OTHER_METHOD,
    }
    assert {
        labels["http_response_status_code"]
        for labels in recorded
        if labels["http_request_method"] == _OTHER_METHOD
    } == {"405"}


def test_every_known_method_is_labelled_as_itself(
    api_client: TestClient, exposition_client: TestClient
) -> None:
    """:data:`_KNOWN_METHODS` is what FastAPI labels as itself (#2931, #3563).

    The literal restates a set FastAPI owns and does not export, so it is pinned
    by behaviour: each method in it, sent to a real route, must come back as its
    own label rather than ``_OTHER``. A method FastAPI stops knowing reddens
    here, and so does a stale entry in the literal.
    """

    before = _request_counts(exposition_client)
    for method in sorted(_KNOWN_METHODS):
        api_client.request(method, "/api/healthz")
    recorded = _new_requests(before, _request_counts(exposition_client))
    assert {labels["http_request_method"] for labels in recorded} == _KNOWN_METHODS


def _declared_methods(routes: Iterable[BaseRoute]) -> frozenset[str]:
    """Every HTTP method the app's routes declare, recursively through mounts.

    Routes are read through :func:`fastapi.routing.iter_route_contexts`, the
    public view of an app's routes with every included router flattened (since
    FastAPI 0.137 ``app.routes`` holds the included routers themselves, not
    their routes).

    WebSocket routes (``@router.websocket``) are excluded rather than
    overlooked: FastAPI's telemetry records no HTTP metrics for a WebSocket
    connection, so such a route contributes no ``http_request_method`` label at
    all. The exclusion is by route type, not by "declares no ``methods``", so
    that a future route class declaring neither methods nor sub-routes is
    reported by the assertion below instead of silently inheriting the
    WebSocket exemption.

    A ``Mount`` is exempted by the type of the app it mounts, not by the shape of
    its ``routes`` (issue #2931). ``Mount.routes`` is
    ``getattr(self._base_app, "routes", [])`` — ``[]`` and never ``None`` — so
    reading it off the ``Mount`` satisfies the assertion below for *any* mounted
    app, and a bare ASGI one would be skipped in silence rather than reported.
    The mounts this app declares are ``StaticFiles`` (``docs-assets``, and the
    SPA when a dist dir is configured), which answers GET and HEAD and 405s every
    other method — both known, and nothing underneath to walk — so those are
    skipped by type; every other mount is walked through the mounted app itself,
    and reaches the assertion when that app has no ``routes`` of its own.
    (``Mount.app``, not the private ``_base_app``: a mount wrapped in
    ``middleware=`` exposes the wrapper, which has no ``routes``, so it too
    reddens rather than passing silently.)
    """

    methods: set[str] = set()
    for context in iter_route_contexts(list(routes)):
        route = context.original_route
        if isinstance(route, WebSocketRoute):
            continue
        if isinstance(route, Mount) and isinstance(route.app, StaticFiles):
            continue
        declared: set[str] | None = context.methods
        walkable = route.app if isinstance(route, Mount) else route
        nested: list[BaseRoute] | None = getattr(walkable, "routes", None)
        assert declared is not None or nested is not None, (
            f"{type(route).__name__} {context.path!r} declares "
            "neither HTTP methods nor sub-routes: this walk would skip it, and "
            "the method label it can produce would go unchecked"
        )
        methods |= set(declared or ())
        methods |= _declared_methods(nested or ())
    return frozenset(methods)


def test_the_route_walk_exempts_static_file_mounts_and_no_other_mount(
    tmp_path: Path,
) -> None:
    """The ``Mount`` exemption in :func:`_declared_methods` is by type (#2931).

    Over a synthetic app on purpose: the real app declares exactly one mount, a
    ``StaticFiles``, so the containment test below exercises the exemption alone
    and would stay green if the walk went back to reading ``Mount.routes`` —
    which is ``[]`` for every mounted app and so exempts them all.

    One assertion per branch: the exempted type, the mount that is walked
    through, and the bare ASGI app that is neither and must be reported rather
    than skipped.
    """

    async def bare_asgi(*_: object) -> None:
        """An ASGI callable with no ``routes`` of its own."""

    router = APIRouter()

    @router.delete("/thing")
    async def thing() -> None:
        return None

    assert _declared_methods([Mount("/static", StaticFiles(directory=tmp_path))]) == (
        frozenset()
    )
    assert _declared_methods([Mount("/sub", routes=router.routes)]) == frozenset(
        {"DELETE"}
    )
    with pytest.raises(
        AssertionError, match="declares neither HTTP methods nor sub-routes"
    ):
        _declared_methods([Mount("/bare", app=bare_asgi)])


def test_every_route_method_is_a_known_method(shared_app: FastAPI) -> None:
    """The app serves no method FastAPI would label ``_OTHER`` (#2872).

    The known-method set is what keeps the method label bounded when an
    unauthenticated client invents a token on the wire (issue #2762). But a set
    that no longer covers the app's *own* routes is the silent case — a route
    serving a method outside it would be served and then labelled ``_OTHER``,
    which is the metrics equivalent of a real route vanishing from the
    per-route breakdown. This asserts containment against the literal, which
    :func:`test_every_known_method_is_labelled_as_itself` ties to FastAPI.
    """

    declared = _declared_methods(shared_app.routes)
    # Not vacuous: an empty walk would satisfy the containment below trivially,
    # so pin two methods this app certainly declares.
    assert {"GET", "POST"} <= declared
    assert declared <= _KNOWN_METHODS, (
        f"the app's routes declare {sorted(declared - _KNOWN_METHODS)}, which "
        "FastAPI's telemetry labels _OTHER: do not serve the method"
    )
