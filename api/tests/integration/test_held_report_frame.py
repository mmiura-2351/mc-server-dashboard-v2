"""A report whose first attempt outlasts its deadline still reaches subscribers.

Review r1 of #4049 (issue #4042): the deadline used to cut the first attempt. A
COMMIT the server had already applied but not yet answered then published no
frame, and the retry, finding the row already at the reported state, staged none
either. The servicer now starts the hold at the deadline and awaits the same
attempt.

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). The real sink writes through a proxy that holds back the COMMIT's
answer past the deadline; the report comes over the real control-plane stream.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import make_url

from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.fleet.adapters import grpc_server
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeClock, FakeDatabasePing
from tests.fleet.test_grpc_session import (
    _GRPC_SERVER_LOGGER,
    _HOLD_APPLIED,
    _HOLD_STARTED,
    _T0,
    _TIMEOUT,
    _WORKER_ID,
    _Harness,
    _logged,
    _RealWriteSink,
    _registered,
    _status_message,
    _wait_until,
)
from tests.integration.test_observed_state_status_events import (  # noqa: F401
    _received,
    _World,
    world,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_DEADLINE_SECONDS = 0.2
# Past the deadline, and well inside the statement bound below.
_LATE_SECONDS = 0.8


class _LateAnswerProxy:
    """Forwards connections to the database, holding back what the server sends
    until ``answer_at`` (a ``time.monotonic`` reading) while that is ahead."""

    def __init__(self, host: str, port: int) -> None:
        self.answer_at = 0.0
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
                if to_client and (wait := self.answer_at - time.monotonic()) > 0:
                    await asyncio.sleep(wait)
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


async def test_a_commit_answered_after_the_deadline_still_reaches_subscribers(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    monkeypatch.setattr(
        grpc_server, "_FIRST_ATTEMPT_TIMEOUT_SECONDS", _DEADLINE_SECONDS
    )
    assert _DB_URL is not None
    server = await world.server("survival", worker=uuid.UUID(_WORKER_ID))
    target = make_url(_DB_URL)
    assert target.host is not None and target.port is not None
    proxy = _LateAnswerProxy(target.host, target.port)
    listener = await asyncio.start_server(proxy.serve, "127.0.0.1", 0)
    url = target.set(host="127.0.0.1", port=listener.sockets[0].getsockname()[1])
    engine = create_engine(
        url.render_as_string(hide_password=False), statement_timeout=10.0
    )

    def _answer_late(_conn: object) -> None:
        # Fires just before the COMMIT is sent: the server applies it at once,
        # and its answer comes back only after the deadline.
        proxy.answer_at = time.monotonic() + _LATE_SECONDS

    sa_event.listen(engine.sync_engine, "commit", _answer_late)
    sink = ServersServerStateSink(
        create_session_factory(engine), clock=world.clock, real_time_events=world.bus
    )
    watching = world.watch(server)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_RealWriteSink(sink),
        database_ping=FakeDatabasePing(reachable=True),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(str(server.id.value), pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: _logged(caplog, _HOLD_APPLIED), "the report was not applied"
        )
        frames = [event.payload for event in await _received(watching)]
        await call.done_writing()
    finally:
        await h.stop()
        await engine.dispose()
        listener.close()
        for writer in proxy.writers:
            writer.close()

    assert _logged(caplog, _HOLD_STARTED) != []
    assert (await world.observed(server)).value == "running"
    assert frames == [{"state": "running", "detail": "", "reason": ""}]
