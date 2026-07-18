"""Servers-backed adapter for the fleet :class:`ServerStateSink` Port.

The control-plane event path (the gRPC servicer, a fleet adapter) reconciles
authoritative *server* state from worker reports — observed-state caching on a
``StatusChange``, observed=unknown on disconnect (FR-WRK-4), and the
running-server tally that rebuilds a reconnected worker's placement load (epic #7
obligation). The servicer depends on the fleet-domain Port; this adapter fulfils
it against the servers repository, opening its own transaction per call from the
injected session factory (the servicer has no request-scoped UnitOfWork).

This is an adapter-layer composition across contexts: a fleet Port implemented
with the servers repository. The servers *domain*/*application* never reach into
fleet (import-linter); only this edge module bridges the two.

It is also where the Bedrock relay tunnel lifecycle is driven (issue #1544): a
``StatusChange`` report is the API's one authoritative signal that a server's
observed state changed, so it is the natural single hook for both directions —
``OpenBedrockTunnel`` when a Bedrock-enabled server's freshest known state is
``running``, ``CloseBedrockTunnel`` for every other freshest known state
(starting/stopping/stopped/restarting/crashed). Gating on "freshest known"
(the repository's existing monotonic write guard, issue #216) matters here: an
out-of-order/stale report must not flip the tunnel the wrong way.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.fleet.adapters.control_plane import GrpcControlPlane
from mc_server_dashboard_api.fleet.adapters.relay_state import (
    BedrockTunnelTable,
    RelayRegistration,
)
from mc_server_dashboard_api.fleet.domain.server_state_sink import ServerStateSink
from mc_server_dashboard_api.servers.adapters.bedrock_tunnel_sync import (
    BedrockTunnelSyncer,
)
from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.value_objects import (
    ObservedState,
    ServerId,
    WorkerId,
)

_LOG = logging.getLogger(__name__)


def _parse_id(value: str, *, kind: str) -> uuid.UUID | None:
    """Parse an id the seam guarantees is a UUID, logging loudly on failure.

    Worker ids are enforced to be UUIDs at registration (issue #99) and server
    ids are DB-issued UUIDs, so a value that fails to parse here is an invariant
    violation at the control-plane seam. It is logged at ERROR (not silently
    skipped) so the broken bridging surfaces instead of dropping reports.
    """

    try:
        return uuid.UUID(value)
    except ValueError:
        _LOG.error(
            "control-plane %s is not a UUID; dropping report (invariant violation)",
            kind,
            extra={kind: value},
        )
        return None


class ServersServerStateSink(ServerStateSink):
    """:class:`ServerStateSink` adapter writing through the servers repository."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Clock,
        control_plane: GrpcControlPlane | None = None,
        relay_registration: RelayRegistration | None = None,
        bedrock_tunnel_table: BedrockTunnelTable | None = None,
        bedrock_tunnel_port: int = 0,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        # Bedrock tunnel dispatch dependencies (issue #1544). Optional so a
        # caller that does not care about the Bedrock relay path (e.g. a unit
        # test exercising only the parse-failure guards) need not supply them;
        # left unset, record_observed_state's bedrock sync is a no-op.
        # Internally builds a BedrockTunnelSyncer when all deps are present
        # (issue #1602: the syncer is now shared with the lifecycle Port).
        self._syncer: BedrockTunnelSyncer | None = None
        if control_plane is not None and bedrock_tunnel_table is not None:
            assert relay_registration is not None
            self._syncer = BedrockTunnelSyncer(
                session_factory,
                control_plane=control_plane,
                relay_registration=relay_registration,
                bedrock_tunnel_table=bedrock_tunnel_table,
                bedrock_tunnel_port=bedrock_tunnel_port,
            )

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str
    ) -> bool:
        parsed = _parse_id(server_id, kind="server_id")
        parsed_worker = _parse_id(worker_id, kind="worker_id")
        if parsed is None or parsed_worker is None:
            return False
        observed = ObservedState(state)
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            server = await repo.get_by_id(ServerId(parsed))
            if server is None:
                return False
            # Ownership guard: only the server's currently assigned worker may
            # write its observed state. A report from any other worker (stale or
            # misrouted) is dropped with a warning, not applied (defense-in-depth).
            # This snapshot check is diagnostic only — the authoritative check is
            # the expected_worker condition inside the guarded UPDATE below
            # (issue #1708), which cannot be raced by a reassignment committing
            # after this read.
            if server.assigned_worker_id != WorkerId(parsed_worker):
                _LOG.warning(
                    "dropping status report from non-owning worker",
                    extra={
                        "server_id": server_id,
                        "reporting_worker_id": worker_id,
                        "assigned_worker_id": (
                            None
                            if server.assigned_worker_id is None
                            else str(server.assigned_worker_id.value)
                        ),
                    },
                )
                return False
            # The sink NEVER unassigns from a status report (issue #847). The old
            # #217 sink-unassign (clear the assignment when the owning worker
            # reports stopped under desired=stopped) raced the final-snapshot
            # window: post-#847 the worker is STILL the owner while the final
            # snapshot uploads, so its terminal StatusChange(stopped) passed the
            # ownership guard above and released the assignment milliseconds into a
            # snapshot that can last minutes — reopening the stop->re-place
            # generation race the #847 hold exists to close. The stop flow now owns
            # the unassign end-to-end: StopServer clears the assignment only AFTER
            # the final snapshot settles, and the reconciler's stale-stop arm
            # recovers a row left wedged at (stopped, stopped, assigned) by a
            # crash/timeout mid-window (the deliberate replacement for #217's
            # recovery). So this sink only caches the observed state.
            applied = await repo.record_observed_state(
                ServerId(parsed),
                observed,
                self._clock.now(),
                unassign=False,
                # Assert the reporting worker inside the UPDATE's WHERE (issue
                # #1708): a stop-unassign + re-place to another worker committing
                # between the snapshot read above and this write must drop this
                # (freshest-stamped) report instead of overwriting the new
                # owner's state. Zero rows matched -> applied=False, which also
                # skips the Bedrock tunnel sync below (a dropped write must not
                # flip the tunnel).
                expected_worker=WorkerId(parsed_worker),
            )
            await session.commit()
            if applied and server.bedrock_port is not None and self._syncer is not None:
                await self._syncer.sync_with_session(
                    session=session,
                    server_id=ServerId(parsed),
                    bedrock_port=server.bedrock_port,
                    worker_id=WorkerId(parsed_worker),
                    running=observed is ObservedState.RUNNING,
                )
            return applied

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        parsed = _parse_id(worker_id, kind="worker_id")
        if parsed is None:
            return
        # A worker disconnect deliberately does NOT invalidate any Bedrock tunnel
        # credential (issue #1544). The tunnel is a worker-initiated QUIC
        # connection independent of the control-plane stream: a control-plane blip
        # (the trigger for this call) must not tear down a still-healthy tunnel,
        # and the worker redials with the SAME token on reconnect (#1546), which
        # the whole-lifetime token validity in BedrockTunnelTable is built for. A
        # credential rotates only on a genuine stop/crash (an observed-state leave
        # of running -> CloseBedrockTunnel), never on a mere disconnect.
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            await repo.mark_worker_servers_unknown(WorkerId(parsed), self._clock.now())
            await session.commit()

    async def existing_server_ids(self, *, server_ids: list[str]) -> set[str]:
        # Unparseable (non-UUID) IDs are treated as existing so the caller never
        # classifies them as deleted. ScanHeldServers advertises any content-
        # bearing dir in the scratch root (not just UUIDs), so a safety copy like
        # "<id>.bak" could be advertised; misclassifying it would cause the worker
        # to RemoveAll it (issue #924 review).
        unparseable: set[str] = set()
        valid: list[uuid.UUID] = []
        for sid in server_ids:
            parsed = _parse_id(sid, kind="server_id")
            if parsed is None:
                unparseable.add(sid)
            else:
                valid.append(parsed)
        if not valid:
            return unparseable
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            existing = await repo.existing_ids([ServerId(v) for v in valid])
            return {str(sid.value) for sid in existing} | unparseable

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        parsed = _parse_id(worker_id, kind="worker_id")
        if parsed is None:
            return {}
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            return await repo.running_assignment_ids_for_worker(WorkerId(parsed))
