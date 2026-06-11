"""Servers-backed adapter for the fleet :class:`LateSnapshotResultSink` Port.

The control-plane result path (the gRPC servicer, a fleet adapter) recognises a
final-snapshot ``CommandResult`` that arrives after its dispatch timed out and
abandoned the pending future (issue #891) — a ``TRANSFER_FAILED`` once the
worker's transfer bound aborts the upload (#874/#890), or a late SUCCESS. Rather
than drop it and wait out the reconciler grace, it hands the (server, worker,
outcome) to this Port so the held (stopped, stopped, assigned) row is released
immediately.

The servicer depends on the fleet-domain Port; this adapter fulfils it against the
``StopServer`` use case's guarded clear, building a fresh use case per call from
the injected session factory (the servicer has no request-scoped UnitOfWork) —
mirroring :class:`ServersServerStateSink`. The servers *domain*/*application*
never reach into fleet (import-linter); only this edge module bridges the two.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.fleet.domain.late_snapshot_sink import (
    LateSnapshotResultSink,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.servers.application.lifecycle import StopServer
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import ControlPlane
from mc_server_dashboard_api.servers.domain.value_objects import ServerId, WorkerId

_LOG = logging.getLogger(__name__)


def _parse_id(value: str, *, kind: str) -> uuid.UUID | None:
    """Parse an id the seam guarantees is a UUID, logging loudly on failure.

    Worker ids are enforced to be UUIDs at registration (issue #99) and server
    ids are DB-issued UUIDs, so a value that fails to parse here is an invariant
    violation at the control-plane seam. It is logged at ERROR (not silently
    skipped) so the broken bridging surfaces instead of dropping the clear.
    """

    try:
        return uuid.UUID(value)
    except ValueError:
        _LOG.error(
            "control-plane %s is not a UUID; dropping late-snapshot clear "
            "(invariant violation)",
            kind,
            extra={kind: value},
        )
        return None


class ServersLateSnapshotResultSink(LateSnapshotResultSink):
    """:class:`LateSnapshotResultSink` adapter over the ``StopServer`` use case."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        control_plane: ControlPlane,
        clock: Clock,
    ) -> None:
        # A fresh UnitOfWork is built per call so the servicer (no request UoW) never
        # shares a session across concurrent results. The control plane and clock are
        # stateless seams reused across calls; the clear path dispatches no command
        # (the server is already stopped), so the control plane is only the
        # StopServer constructor's required dependency.
        self._session_factory = session_factory
        self._control_plane = control_plane
        self._clock = clock

    async def clear_held_assignment_on_late_snapshot(
        self, *, server_id: str, worker_id: str, succeeded: bool
    ) -> None:
        parsed = _parse_id(server_id, kind="server_id")
        parsed_worker = _parse_id(worker_id, kind="worker_id")
        if parsed is None or parsed_worker is None:
            return
        stop_server = StopServer(
            uow=SqlAlchemyUnitOfWork(self._session_factory),
            control_plane=self._control_plane,
            clock=self._clock,
        )
        await stop_server.clear_assignment_after_late_snapshot(
            server_id=ServerId(parsed),
            worker_id=WorkerId(parsed_worker),
            succeeded=succeeded,
        )
