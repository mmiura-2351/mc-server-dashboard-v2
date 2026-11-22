"""The reconciler marks a gone Worker's servers ``unknown`` when its teardown did not.

Issue #4130: only a session's teardown marks a disconnected Worker's servers
``unknown``. A teardown whose write fails, or one skipped under the refused
registration's bound (issue #4091), left them reading their last report, for
example ``running``, for as long as the Worker stayed away. The reconciler tick
now marks them once the Worker has had no session for the grace.

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). The Worker registers and reports over the real control-plane
stream, into the real database; only the teardown's write is made to fail or
stall.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from typing import Any

import grpc
import pytest

from mc_server_dashboard_api.fleet.adapters import grpc_server
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.control_plane import (
    Command as FleetCommand,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    CommandResult,
)
from mc_server_dashboard_api.fleet.domain.control_plane import (
    ControlPlane as FleetControlPlane,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.application.stop_dispatch_refusals import (
    StopDispatchRefusals,
)
from mc_server_dashboard_api.servers.application.unrecorded_stops import (
    UnrecordedStops,
)
from mc_server_dashboard_api.servers.domain.control_plane import ControlPlane
from mc_server_dashboard_api.servers.domain.value_objects import (
    ObservedState,
    WorkerId,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeServerStateSink
from tests.fleet.test_grpc_session import (
    _CREDENTIAL,
    _T0,
    _TIMEOUT,
    _auth,
    _Harness,
    _register_message,
    _registered,
    _status_message,
    _wait_until,
)
from tests.integration.test_lifecycle_scenarios import _load
from tests.integration.test_observed_state_status_events import (  # noqa: F401
    _received,
    _status,
    _World,
    world,
)
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_WORKER = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ANSWER_GUARD_SECONDS = 10.0
# Well past every instant the test reads from the world's clock, which
# advances one second a read, and so past any grace from the session's end.
_TICK_AT = _T0 + dt.timedelta(hours=1)


class _FailingTeardownSink(FakeServerStateSink):
    """Reports through the real sink; every teardown's mark-unknown fails or stalls.

    With ``refuse_second``, the second registration's rebuild fails, so that
    registration is refused. Its teardown's mark-unknown then stalls, and the
    bound on a refused registration's write (issue #4091) skips it.
    """

    def __init__(self, real: ServersServerStateSink, *, refuse_second: bool) -> None:
        super().__init__()
        self._real = real
        self._refuse_second = refuse_second
        self._rebuilds = 0
        self.applied: list[bool] = []
        self.mark_calls = 0
        self.mark_ended = asyncio.Event()

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        self._rebuilds += 1
        if self._refuse_second and self._rebuilds == 2:
            raise RuntimeError("the rebuild failed")
        return {}

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        applied = await self._real.record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )
        self.applied.append(applied)
        return applied

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        self.mark_calls += 1
        try:
            if self._refuse_second:
                await asyncio.Event().wait()
            raise RuntimeError("the database is down")
        finally:
            self.mark_ended.set()


class _NoDispatchFleet(FleetControlPlane):
    """Nothing in these tests is owed a command: the Worker is gone."""

    async def dispatch(
        self,
        *,
        worker_id: FleetWorkerId,
        server_id: str,
        command: FleetCommand,
        timeout_override: float | None = None,
    ) -> CommandResult:
        raise AssertionError(f"unexpected dispatch of {command!r}")


def _reconciler(
    world: _World,  # noqa: F811
    control_plane: ControlPlane,
    clock: FakeClock,
) -> RunReconcilerTick:
    """The tick as the composition root builds it."""

    stop_refusals = StopDispatchRefusals()
    unrecorded_stops = UnrecordedStops()
    return RunReconcilerTick(
        uow=world.uow(),
        make_start_server=lambda: StartServer(
            uow=world.uow(),
            control_plane=control_plane,
            clock=clock,
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(),
            file_store=FakeFileStore(seed_eula=True),
        ),
        make_stop_server=lambda: StopServer(
            uow=world.uow(),
            control_plane=control_plane,
            clock=clock,
            stop_refusals=stop_refusals,
            unrecorded_stops=unrecorded_stops,
        ),
        control_plane=control_plane,
        store_generation=FakeStoreGenerationReader(),
        clock=clock,
        stop_refusals=stop_refusals,
        unrecorded_stops=unrecorded_stops,
        grace_seconds=660,
        held_start_grace_seconds=90,
        refused_stop_grace_seconds=30,
        restart_timeout_seconds=630,
        heartbeat_timeout_seconds=int(_TIMEOUT.total_seconds()),
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
        listener_started_at=_T0 - dt.timedelta(days=1),
    )


@pytest.mark.parametrize("teardown", ["failed", "skipped"])
async def test_a_gone_workers_servers_end_unknown_when_its_teardown_wrote_nothing(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    teardown: str,
) -> None:
    # Held for the whole test: the refused registration's teardown reads it
    # when it runs, after the session has ended.
    monkeypatch.setattr(grpc_server, "_REGISTRATION_DATABASE_TIMEOUT_SECONDS", 0.2)
    server = await world.server("survival", worker=_WORKER)
    server_id = str(server.id.value)
    sink = _FailingTeardownSink(world.sink(), refuse_second=teardown == "skipped")
    clock = world.fleet_clock()
    registry = InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)
    h = _Harness(registry, clock, state_sink=sink)
    try:
        stub = await h.start()
        old = await _registered(stub)
        await old.write(_status_message(server_id, pb.SERVER_STATE_RUNNING))
        await _wait_until(lambda: sink.applied == [True], "the report was not applied")
        if teardown == "skipped":
            # A reconnect supersedes the session, so that session's teardown
            # writes nothing, and the reconnect's registration is refused.
            refused = stub.Session(metadata=_auth(_CREDENTIAL))
            await refused.write(_register_message())
            code = await asyncio.wait_for(refused.code(), timeout=_ANSWER_GUARD_SECONDS)
            assert code is grpc.StatusCode.UNAVAILABLE
        await old.done_writing()
        await asyncio.wait_for(old.code(), timeout=_ANSWER_GUARD_SECONDS)
        await asyncio.wait_for(sink.mark_ended.wait(), timeout=_ANSWER_GUARD_SECONDS)
        assert h.servicer is not None
        await h.servicer.drain_teardowns()
    finally:
        await h.stop()
    assert sink.mark_calls == 1
    assert await world.observed(server) is ObservedState.RUNNING
    watching = world.watch(server)

    control_plane = FleetControlPlaneAdapter(
        registry=registry, control_plane=_NoDispatchFleet()
    )
    await _reconciler(world, control_plane, FakeClock(_TICK_AT)).tick()

    assert await world.observed(server) is ObservedState.UNKNOWN
    assert [event.payload for event in await _received(watching)] == [
        _status("unknown")
    ]


async def test_the_mark_keeps_a_report_newer_than_the_sessions_end(
    world: _World,  # noqa: F811
) -> None:
    # The write's guard on PostgreSQL: a server observed after the session's
    # end is kept, and one observed before it is marked and announced. The
    # kept one leaves the Worker selected, and the write it repeats on the
    # next tick changes nothing and announces nothing.
    worker = uuid.uuid4()
    before = await world.server("before", worker=worker, reported="running")
    ended_at = world.clock.now()
    after = await world.server("after", worker=worker, reported="running")
    control_plane = FakeControlPlane()
    control_plane.end_session(WorkerId(worker), at=ended_at)
    reconciler = _reconciler(world, control_plane, FakeClock(_TICK_AT))
    watching = world.watch_all()

    await reconciler.tick()

    marked = await _load(world.engine, before.id)
    kept = await _load(world.engine, after.id)
    assert marked is not None and kept is not None
    assert (marked.observed_state, marked.observed_at) == (
        ObservedState.UNKNOWN,
        ended_at,
    )
    assert kept.observed_state is ObservedState.RUNNING
    assert [event.payload for event in await _received(watching)] == [
        _status("unknown")
    ]
    world.assert_published_after_commit()

    watching = world.watch_all()
    await reconciler.tick()

    assert await _load(world.engine, before.id) == marked
    assert await _load(world.engine, after.id) == kept
    assert await _received(watching) == []
