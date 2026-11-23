"""A session's teardown write that lands late keeps the next session's reports.

Issue #4092: a session's teardown checks that the session is still current and
then awaits the write that marks the Worker's servers ``unknown``. A database
slow to answer delays that write past the Worker's next registration, and the
write used to land after the new session's reports, leaving a running server
``unknown``. The write now leaves a server whose observation is newer than its
own stamp, so a report stamped after it survives whichever commits first.

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). The teardown writes through a proxy that holds back what the server
sends while the test stalls it, so the write's statements wait unsent; the
registrations and reports go to the database directly. All of them come over
the real control-plane stream, and both sinks share one clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
from sqlalchemy import make_url

from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.domain.value_objects import ObservedState
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeClock, FakeServerStateSink
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
from tests.integration.test_observed_state_status_events import (  # noqa: F401
    _World,
    world,
)
from tests.integration.test_registration_bound import _StallingProxy

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_WORKER = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ANSWER_GUARD_SECONDS = 10.0


class _StallableTeardownSink(FakeServerStateSink):
    """Registers and reports through the real sink, and writes each teardown's
    mark-unknown through one whose database the test can stall.

    With ``refuse_first``, the first registration's rebuild fails, so that
    registration is refused. It is not refused at its deadline: the teardown of
    a refused registration writes under that deadline too (issue #4091), and
    this write has to outlast the next session's report.
    """

    def __init__(
        self,
        real: ServersServerStateSink,
        stallable: ServersServerStateSink,
        *,
        refuse_first: bool,
    ) -> None:
        super().__init__()
        self._real = real
        self._stallable = stallable
        self._refuse_first = refuse_first
        self.applied: list[bool] = []
        self.marking = asyncio.Event()
        self.marked = asyncio.Event()
        self.mark_failures: list[BaseException] = []

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        if self._refuse_first:
            self._refuse_first = False
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
        self.marking.set()
        try:
            await self._stallable.mark_worker_servers_unknown(worker_id=worker_id)
        except BaseException as exc:
            self.mark_failures.append(exc)
            raise
        finally:
            self.marked.set()


@contextlib.asynccontextmanager
async def _stallable_teardowns(
    world: _World,  # noqa: F811
    *,
    refuse_first: bool = False,
) -> AsyncIterator[tuple[_Harness, _StallableTeardownSink, _StallingProxy]]:
    assert _DB_URL is not None
    target = make_url(_DB_URL)
    assert target.host is not None and target.port is not None
    proxy = _StallingProxy(target.host, target.port)
    listener = await asyncio.start_server(proxy.serve, "127.0.0.1", 0)
    url = target.set(host="127.0.0.1", port=listener.sockets[0].getsockname()[1])
    # No connection is pooled before the stall, so the teardown's write waits
    # in its connect, before any of its statements reaches the server.
    engine = create_engine(url.render_as_string(hide_password=False))
    sink = _StallableTeardownSink(
        world.sink(),
        ServersServerStateSink(
            create_session_factory(engine),
            clock=world.clock,
            real_time_events=world.bus,
        ),
        refuse_first=refuse_first,
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        world.fleet_clock(),
        state_sink=sink,
    )
    try:
        yield h, sink, proxy
    finally:
        proxy.answering.set()
        await h.stop()
        if h.servicer is not None:
            await h.servicer.drain_teardowns()
        await engine.dispose()
        listener.close()
        for writer in proxy.writers:
            writer.close()


@pytest.mark.parametrize("ending", ["closed", "refused"])
async def test_a_teardown_write_landing_after_the_next_sessions_report_keeps_it(
    world: _World,  # noqa: F811
    ending: str,
) -> None:
    server = await world.server("survival", worker=_WORKER)
    server_id = str(server.id.value)
    async with _stallable_teardowns(world, refuse_first=ending == "refused") as (
        h,
        sink,
        proxy,
    ):
        stub = await h.start()
        proxy.answering.clear()
        if ending == "closed":
            old = await _registered(stub)
            await old.write(_status_message(server_id, pb.SERVER_STATE_RUNNING))
            await _wait_until(
                lambda: sink.applied == [True],
                "the old session's report was not applied",
            )
            await old.done_writing()
        else:
            old = stub.Session(metadata=_auth(_CREDENTIAL))
            await old.write(_register_message())
            code = await asyncio.wait_for(old.code(), timeout=_ANSWER_GUARD_SECONDS)
            assert code is grpc.StatusCode.UNAVAILABLE
        await asyncio.wait_for(sink.marking.wait(), timeout=_ANSWER_GUARD_SECONDS)
        reports = len(sink.applied)

        new = await _registered(stub)
        await new.write(_status_message(server_id, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: sink.applied[reports:] == [True],
            "the new session's report was not applied",
        )
        proxy.answering.set()
        await asyncio.wait_for(sink.marked.wait(), timeout=_ANSWER_GUARD_SECONDS)
        observed = await world.observed(server)
        await new.done_writing()

    assert sink.mark_failures == []
    assert observed is ObservedState.RUNNING


async def test_a_late_teardown_write_still_marks_the_disconnected_workers_servers(
    world: _World,  # noqa: F811
) -> None:
    # No newer session: the Worker is gone, and its servers go unknown however
    # late the write lands (FR-WRK-4).
    server = await world.server("survival", worker=_WORKER)
    async with _stallable_teardowns(world) as (h, sink, proxy):
        stub = await h.start()
        proxy.answering.clear()
        old = await _registered(stub)
        await old.write(_status_message(str(server.id.value), pb.SERVER_STATE_RUNNING))
        await _wait_until(lambda: sink.applied == [True], "the report was not applied")
        await old.done_writing()
        await asyncio.wait_for(sink.marking.wait(), timeout=_ANSWER_GUARD_SECONDS)

        proxy.answering.set()
        await asyncio.wait_for(sink.marked.wait(), timeout=_ANSWER_GUARD_SECONDS)
        observed = await world.observed(server)

    assert sink.mark_failures == []
    assert observed is ObservedState.UNKNOWN
