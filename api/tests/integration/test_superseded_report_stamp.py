"""A superseded session's report still being written lands behind what came after.

Issue #4118: a report's first attempt is not re-checked against the current
session once it has started, so a superseded session's attempt can land after
the newer session's reports, or after the newer session's teardown. It used to
be stamped as it was written, after its read of the server, so it overwrote the
newer state, or kept a disconnected Worker's server reading its old state
through both the teardown and the reconciler's backstop (issue #4130). A report
is now stamped as the API receives it, so the monotonic guard orders it behind
everything the newer session wrote.

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). Everything comes over the real control-plane stream into the real
database. The servicer, the registry and the sink share the world's clock, as
they share the wall clock in production.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import uuid
from typing import Any

import grpc
import pytest

from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    ObservedState,
    ServerId,
    WorkerId,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeClock as FleetFakeClock
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
from tests.integration.test_dropped_worker_backstop import (
    _TICK_AT,
    _NoDispatchFleet,
    _reconciler,
)
from tests.integration.test_observed_state_status_events import (  # noqa: F401
    _World,
    world,
)
from tests.servers.fakes import FakeClock

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_WORKER = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ANSWER_GUARD_SECONDS = 10.0
_BACKSTOP_MARKED = "marked them unknown"


class _WorldFleetClock(FleetFakeClock):
    """The world's clock, for the servicer and the registry."""

    def __init__(self, world: _World) -> None:  # noqa: F811
        super().__init__(_T0)
        self._world = world

    def now(self) -> dt.datetime:
        return self._world.clock.now()


class _Sink(FakeServerStateSink):
    """Writes through the real sink; with ``refuse_next``, the next
    registration's rebuild fails, so that registration is refused."""

    def __init__(self, real: ServersServerStateSink) -> None:
        super().__init__()
        self._real = real
        self.refuse_next = False
        self.applied: list[bool] = []
        self.marked = asyncio.Event()

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        if self.refuse_next:
            self.refuse_next = False
            raise RuntimeError("the rebuild failed")
        return await self._real.running_assignment_ids(worker_id=worker_id)

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        applied = await self._real.record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )
        self.applied.append(applied)
        return applied

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        await self._real.mark_worker_servers_unknown(worker_id=worker_id)
        self.marked.set()


class _Gate:
    """Holds the next call that passes it, until released."""

    def __init__(self) -> None:
        self.armed = False
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self) -> None:
        if self.armed:
            self.armed = False
            self.reached.set()
            await self.release.wait()


def _gate_reads_and_marks(monkeypatch: pytest.MonkeyPatch) -> tuple[_Gate, _Gate]:
    """Gates a server read once it has answered, and a mark-unknown once its
    stamp is taken."""

    read_gate, mark_gate = _Gate(), _Gate()
    read = SqlAlchemyServerRepository.get_by_id
    mark = SqlAlchemyServerRepository.mark_worker_servers_unknown

    async def _read(repo: SqlAlchemyServerRepository, server_id: ServerId) -> Any:
        server = await read(repo, server_id)
        await read_gate.hold()
        return server

    async def _mark(
        repo: SqlAlchemyServerRepository, worker_id: WorkerId, observed_at: dt.datetime
    ) -> None:
        await mark_gate.hold()
        await mark(repo, worker_id, observed_at)

    monkeypatch.setattr(SqlAlchemyServerRepository, "get_by_id", _read)
    monkeypatch.setattr(
        SqlAlchemyServerRepository, "mark_worker_servers_unknown", _mark
    )
    return read_gate, mark_gate


async def _supersede_a_report_in_flight(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    *,
    ending: str,
) -> tuple[InMemoryWorkerRegistry, ObservedState]:
    """The old session's report is read, then held while the Worker comes back.

    ``reported``: the newer session reports ``running``, then the old report
    lands. ``marked``: the newer registration is refused, its teardown marks
    the server ``unknown``, then the old report lands. ``stamped``: the same,
    except that the old report lands after the teardown has stamped its write
    and before that write. Returns the registry, and the server's state once
    both writes have landed.
    """

    server = await world.server("survival", worker=_WORKER)
    server_id = str(server.id.value)
    read_gate, mark_gate = _gate_reads_and_marks(monkeypatch)
    sink = _Sink(world.sink())
    clock = _WorldFleetClock(world)
    registry = InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)
    h = _Harness(registry, clock, state_sink=sink)
    try:
        stub = await h.start()
        old = await _registered(stub)
        read_gate.armed = True
        stale = pb.SERVER_STATE_RUNNING
        if ending == "reported":
            stale = pb.SERVER_STATE_STARTING
        await old.write(_status_message(server_id, stale))
        await asyncio.wait_for(read_gate.reached.wait(), timeout=_ANSWER_GUARD_SECONDS)

        if ending == "reported":
            new = await _registered(stub)
            await new.write(_status_message(server_id, pb.SERVER_STATE_RUNNING))
            await _wait_until(
                lambda: sink.applied == [True], "the new session's report not applied"
            )
        else:
            mark_gate.armed = ending == "stamped"
            sink.refuse_next = True
            new = stub.Session(metadata=_auth(_CREDENTIAL))
            await new.write(_register_message())
            teardown = mark_gate.reached if ending == "stamped" else sink.marked
            await asyncio.wait_for(teardown.wait(), timeout=_ANSWER_GUARD_SECONDS)

        # The old stream closes cleanly, so nothing cancels its attempt.
        await old.done_writing()
        read_gate.release.set()
        await _wait_until(
            lambda: len(sink.applied) == (2 if ending == "reported" else 1),
            "the old session's report did not land",
        )
        mark_gate.release.set()
        await asyncio.wait_for(old.code(), timeout=_ANSWER_GUARD_SECONDS)
        if ending != "reported":
            await asyncio.wait_for(sink.marked.wait(), timeout=_ANSWER_GUARD_SECONDS)
            code = await asyncio.wait_for(new.code(), timeout=_ANSWER_GUARD_SECONDS)
            assert code is grpc.StatusCode.UNAVAILABLE
        # Read while the newer session lives: its own teardown marks unknown.
        observed = await world.observed(server)
        if ending == "reported":
            await new.done_writing()
        assert h.servicer is not None
        await h.servicer.drain_teardowns()
    finally:
        read_gate.release.set()
        mark_gate.release.set()
        await h.stop()
    return registry, observed


async def test_a_superseded_sessions_late_report_leaves_the_newer_report(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, observed = await _supersede_a_report_in_flight(
        world, monkeypatch, ending="reported"
    )

    assert observed is ObservedState.RUNNING


@pytest.mark.parametrize("ending", ["marked", "stamped"])
async def test_a_superseded_sessions_late_report_does_not_outlive_the_disconnect(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ending: str,
) -> None:
    # The Worker has no session once the refused registration's teardown has
    # run, so its server ends unknown, whichever of the two writes lands
    # first. Landing after the teardown's write, the old report must not set
    # it back to running; landing between the teardown's stamp and its write,
    # it was read before that stamp, so the write marks it. Nor does the
    # reconciler's backstop then select the Worker on every tick for a write
    # that changes nothing (issue #4144).
    registry, observed = await _supersede_a_report_in_flight(
        world, monkeypatch, ending=ending
    )

    assert observed is ObservedState.UNKNOWN
    control_plane = FleetControlPlaneAdapter(
        registry=registry, control_plane=_NoDispatchFleet()
    )
    caplog.set_level(logging.INFO)
    reconciler = _reconciler(world, control_plane, FakeClock(_TICK_AT))
    await reconciler.tick()
    await reconciler.tick()
    assert [r for r in caplog.records if _BACKSTOP_MARKED in r.getMessage()] == []
