"""A registration the database stalls is refused in time, and the next succeeds.

Issue #4050: a Worker's registration rebuilds its assignments from the database
before the API acks it, and that read was bounded only by the driver's statement
bound, 60 s by default, past the 30 s the Worker waits for the ack. The servicer
now ends the registration with ``UNAVAILABLE`` at its own deadline, which the
Worker retries with backoff.

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). The real sink reads through a proxy that holds back what the server
sends while the test stalls it, on an engine whose driver bounds are far past the
deadline; the registrations come over the real control-plane stream.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import grpc
import pytest
from sqlalchemy import make_url

from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.fleet.adapters import grpc_server
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from tests.fleet.fakes import FakeClock, FakeServerStateSink
from tests.fleet.test_grpc_session import (
    _CREDENTIAL,
    _T0,
    _TIMEOUT,
    _WORKER_ID,
    _auth,
    _Harness,
    _register_message,
)
from tests.integration.test_observed_state_status_events import (  # noqa: F401
    _World,
    world,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_DEADLINE_SECONDS = 0.5
# The driver's bounds: far past the deadline, so only the deadline can end the
# stalled read.
_DRIVER_BOUND_SECONDS = 60.0
# How long the test waits for an answer: past the deadline and the 2 s SQLAlchemy
# gives the stalled connection to close once the read is cancelled, far short of
# the driver's bounds.
_ANSWER_GUARD_SECONDS = 10.0


class _StallingProxy:
    """Forwards connections to the database, holding back what the server sends
    while ``answering`` is clear."""

    def __init__(self, host: str, port: int) -> None:
        self.answering = asyncio.Event()
        self.answering.set()
        self._target = (host, port)
        self.writers: list[asyncio.StreamWriter] = []

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        to_client: bool,
    ) -> None:
        try:
            while data := await reader.read(65536):
                if to_client:
                    await self.answering.wait()
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def serve(
        self, client: asyncio.StreamReader, to_client: asyncio.StreamWriter
    ) -> None:
        upstream, to_upstream = await asyncio.open_connection(*self._target)
        self.writers += [to_client, to_upstream]
        await asyncio.gather(
            self._pump(client, to_upstream, to_client=False),
            self._pump(upstream, to_client, to_client=True),
        )


class _RealRegistrationSink(FakeServerStateSink):
    """Runs the registration's rebuild, and the teardown's write, through the
    real sink."""

    def __init__(self, real: ServersServerStateSink) -> None:
        super().__init__()
        self._real = real

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        return await self._real.running_assignment_ids(worker_id=worker_id)

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        await self._real.mark_worker_servers_unknown(worker_id=worker_id)


async def test_a_stalled_registration_is_refused_in_time_and_the_next_succeeds(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        grpc_server, "_REGISTRATION_DATABASE_TIMEOUT_SECONDS", _DEADLINE_SECONDS
    )
    assert _DB_URL is not None
    await world.server("survival", worker=uuid.UUID(_WORKER_ID))
    target = make_url(_DB_URL)
    assert target.host is not None and target.port is not None
    proxy = _StallingProxy(target.host, target.port)
    listener = await asyncio.start_server(proxy.serve, "127.0.0.1", 0)
    url = target.set(host="127.0.0.1", port=listener.sockets[0].getsockname()[1])
    engine = create_engine(
        url.render_as_string(hide_password=False),
        connect_timeout=_DRIVER_BOUND_SECONDS,
        statement_timeout=_DRIVER_BOUND_SECONDS,
    )
    real = ServersServerStateSink(
        create_session_factory(engine), clock=world.clock, real_time_events=world.bus
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_RealRegistrationSink(real),
    )
    worker = WorkerId(_WORKER_ID)
    try:
        stub = await h.start()
        # Pool a connection, so the stall meets the rebuild on it, not in a
        # connect.
        await real.running_assignment_ids(worker_id=_WORKER_ID)
        proxy.answering.clear()

        refused = stub.Session(metadata=_auth(_CREDENTIAL))
        started = time.monotonic()
        await refused.write(_register_message())
        code = await asyncio.wait_for(refused.code(), timeout=_ANSWER_GUARD_SECONDS)
        elapsed = time.monotonic() - started
        # Read while the database is still stalled, the teardown's write too.
        refused_status = h.registry.list_workers()[0].status
        refused_outbound = h.control_plane.outbound_for(worker)

        proxy.answering.set()
        accepted = stub.Session(metadata=_auth(_CREDENTIAL))
        await accepted.write(_register_message())
        ack = await asyncio.wait_for(accepted.read(), timeout=_ANSWER_GUARD_SECONDS)
        candidates = h.registry.candidates_for_placement()
        await accepted.done_writing()
    finally:
        proxy.answering.set()
        await h.stop()
        # Both sessions' teardowns write through the engine, as at the app's
        # shutdown.
        if h.servicer is not None:
            await h.servicer.drain_teardowns()
        await engine.dispose()
        listener.close()
        for writer in proxy.writers:
            writer.close()

    assert code is grpc.StatusCode.UNAVAILABLE
    assert elapsed >= _DEADLINE_SECONDS
    assert refused_status is WorkerStatus.OFFLINE
    assert refused_outbound is None
    assert ack.WhichOneof("payload") == "register_ack"
    assert [candidate.load for candidate in candidates] == [1]
