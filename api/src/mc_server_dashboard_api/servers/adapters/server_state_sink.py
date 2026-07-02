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
from mc_server_dashboard_api.fleet.domain.control_plane import (
    CloseBedrockTunnelCommand,
    OpenBedrockTunnelCommand,
    WorkerNotConnectedError,
)
from mc_server_dashboard_api.fleet.domain.server_state_sink import ServerStateSink
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.plugin_repository import (
    SqlAlchemyPluginRepository,
)
from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.plugin import is_geyser_plugin
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
        self._control_plane = control_plane
        self._relay_registration = relay_registration
        self._bedrock_tunnel_table = bedrock_tunnel_table
        self._bedrock_tunnel_port = bedrock_tunnel_port

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str
    ) -> None:
        parsed = _parse_id(server_id, kind="server_id")
        parsed_worker = _parse_id(worker_id, kind="worker_id")
        if parsed is None or parsed_worker is None:
            return
        observed = ObservedState(state)
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            server = await repo.get_by_id(ServerId(parsed))
            if server is None:
                return
            # Ownership guard: only the server's currently assigned worker may
            # write its observed state. A report from any other worker (stale or
            # misrouted) is dropped with a warning, not applied (defense-in-depth).
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
                return
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
                ServerId(parsed), observed, self._clock.now(), unassign=False
            )
            await session.commit()
            if applied and server.bedrock_port is not None:
                await self._sync_bedrock_tunnel(
                    session=session,
                    server_id=ServerId(parsed),
                    bedrock_port=server.bedrock_port,
                    worker_id=WorkerId(parsed_worker),
                    running=observed is ObservedState.RUNNING,
                )

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        parsed = _parse_id(worker_id, kind="worker_id")
        if parsed is None:
            return
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            await repo.mark_worker_servers_unknown(WorkerId(parsed), self._clock.now())
            await session.commit()

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        parsed = _parse_id(worker_id, kind="worker_id")
        if parsed is None:
            return {}
        async with self._session_factory() as session:
            repo = SqlAlchemyServerRepository(session)
            return await repo.running_assignment_ids_for_worker(WorkerId(parsed))

    async def _sync_bedrock_tunnel(
        self,
        *,
        session: AsyncSession,
        server_id: ServerId,
        bedrock_port: int,
        worker_id: WorkerId,
        running: bool,
    ) -> None:
        """Open/close ``server_id``'s Bedrock tunnel to match its freshest state.

        Skipped when the sink was built without the Bedrock dependencies (relay
        disabled), and when every Geyser copy on the server is disabled — a
        disabled Geyser is not listening on its RakNet port, so a tunnel to it
        would sit idle (PM note, issue #1544).
        """

        control_plane = self._control_plane
        bedrock_tunnel_table = self._bedrock_tunnel_table
        if control_plane is None or bedrock_tunnel_table is None:
            return
        plugins = await SqlAlchemyPluginRepository(session).list_for_server(server_id)
        if not any(p.enabled and is_geyser_plugin(p) for p in plugins):
            return
        fleet_worker_id = FleetWorkerId(str(worker_id.value))
        fleet_server_id = str(server_id.value)
        if running:
            await self._open_bedrock_tunnel(
                control_plane=control_plane,
                bedrock_tunnel_table=bedrock_tunnel_table,
                worker_id=fleet_worker_id,
                server_id=fleet_server_id,
                bedrock_port=bedrock_port,
            )
        else:
            await self._close_bedrock_tunnel(
                control_plane=control_plane,
                bedrock_tunnel_table=bedrock_tunnel_table,
                worker_id=fleet_worker_id,
                server_id=fleet_server_id,
            )

    async def _open_bedrock_tunnel(
        self,
        *,
        control_plane: GrpcControlPlane,
        bedrock_tunnel_table: BedrockTunnelTable,
        worker_id: FleetWorkerId,
        server_id: str,
        bedrock_port: int,
    ) -> None:
        registered = (
            self._relay_registration.current()
            if self._relay_registration is not None
            else None
        )
        if registered is None:
            # No relay has registered its tunnel endpoint, so an OpenBedrockTunnel
            # would have nothing to carry (mirrors relay_server.py's ResolveJoin
            # STOPPED-on-no-registration arm).
            _LOG.warning(
                "bedrock tunnel open skipped: no relay registered",
                extra={"server_id": server_id},
            )
            return
        # The Bedrock QUIC tunnel listener is a distinct relay port from the Java
        # TCP tunnel (RELAY.md Section 13's tunnel_port), but the SAME relay
        # process registers both, so the reachable host is shared; only the port
        # differs, and that port is operator-configured on both the relay and the
        # API sides (relay.bedrock_tunnel_port), exactly like game_port/tunnel_port
        # already are (RELAY.md Section 13) -- so it needs no separate self-report
        # over Register.
        relay_host = registered.endpoint.rsplit(":", 1)[0]
        token = bedrock_tunnel_table.open(
            server_id=server_id, bedrock_port=bedrock_port
        )
        try:
            await control_plane.dispatch_fire_and_forget(
                worker_id=worker_id,
                server_id=server_id,
                command=OpenBedrockTunnelCommand(
                    relay_endpoint=f"{relay_host}:{self._bedrock_tunnel_port}",
                    bedrock_port=bedrock_port,
                    token=token,
                    tls_ca_pem=registered.ca_pem,
                ),
            )
        except WorkerNotConnectedError:
            # The reporting worker just spoke on its stream, so this should not
            # happen in practice; treated the same as relay_server.py's analogous
            # guard rather than propagating (issue #776 poison-event handling
            # already covers genuine failures).
            _LOG.info(
                "bedrock tunnel open skipped: worker not connected",
                extra={"server_id": server_id},
            )

    async def _close_bedrock_tunnel(
        self,
        *,
        control_plane: GrpcControlPlane,
        bedrock_tunnel_table: BedrockTunnelTable,
        worker_id: FleetWorkerId,
        server_id: str,
    ) -> None:
        bedrock_tunnel_table.close(server_id=server_id)
        try:
            await control_plane.dispatch_fire_and_forget(
                worker_id=worker_id,
                server_id=server_id,
                command=CloseBedrockTunnelCommand(),
            )
        except WorkerNotConnectedError:
            _LOG.info(
                "bedrock tunnel close skipped: worker not connected",
                extra={"server_id": server_id},
            )
