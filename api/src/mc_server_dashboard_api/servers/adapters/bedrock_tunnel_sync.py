"""Fleet-backed adapter for the servers :class:`BedrockTunnelSync` Port (issue #1602).

The lifecycle's INVALID_STATE convergence writes observed=running through the
repository directly, bypassing the sink's Bedrock-tunnel hook.  This adapter
exposes the open/close logic as a domain Port so the application layer can
invoke it without importing fleet (import-linter).

Two entry points:

- :meth:`sync_observed` — the Port method. Opens its own session for the Geyser
  plugin read, then opens/closes the tunnel.  Called by the lifecycle use cases
  when they write observed=running/stopped directly (INVALID_STATE convergence,
  confirmed-stop convergence).

- :meth:`sync_with_session` — session-taking method.  Called by
  :class:`ServersServerStateSink`, which already holds a session from its
  ``record_observed_state`` read.  Avoids opening a second session per status
  report.
"""

from __future__ import annotations

import logging

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
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.plugin_repository import (
    SqlAlchemyPluginRepository,
)
from mc_server_dashboard_api.servers.domain.bedrock_tunnel import BedrockTunnelSync
from mc_server_dashboard_api.servers.domain.plugin import has_enabled_geyser
from mc_server_dashboard_api.servers.domain.value_objects import ServerId, WorkerId

_LOG = logging.getLogger(__name__)


class BedrockTunnelSyncer(BedrockTunnelSync):
    """Open/close a Bedrock tunnel to match a server's observed state."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        control_plane: GrpcControlPlane,
        relay_registration: RelayRegistration,
        bedrock_tunnel_table: BedrockTunnelTable,
        bedrock_tunnel_port: int,
    ) -> None:
        self._session_factory = session_factory
        self._control_plane = control_plane
        self._relay_registration = relay_registration
        self._bedrock_tunnel_table = bedrock_tunnel_table
        self._bedrock_tunnel_port = bedrock_tunnel_port

    # -- Port method (issue #1602) -------------------------------------------

    async def sync_observed(
        self,
        *,
        server_id: ServerId,
        worker_id: WorkerId,
        bedrock_port: int | None,
        running: bool,
    ) -> None:
        """Open/close the tunnel; safe to call from any writer of observed state.

        Returns early when ``bedrock_port`` is ``None`` (the server has no
        Bedrock allocation).  Opens its own session for the Geyser-plugin read.
        The body is wrapped in a broad ``except`` so a tunnel-sync failure never
        fails the caller's operation (the convergence commit already landed).
        """

        if bedrock_port is None:
            return
        try:
            async with self._session_factory() as session:
                await self._sync(
                    session=session,
                    server_id=server_id,
                    bedrock_port=bedrock_port,
                    worker_id=worker_id,
                    running=running,
                )
        except Exception:
            _LOG.error(
                "bedrock tunnel sync failed for server %s (issue #1602); "
                "the observed-state write already committed — tunnel state "
                "may be stale until the next StatusChange",
                server_id.value,
                exc_info=True,
            )

    # -- Session-taking method (used by the sink) ----------------------------

    async def sync_with_session(
        self,
        *,
        session: AsyncSession,
        server_id: ServerId,
        bedrock_port: int,
        worker_id: WorkerId,
        running: bool,
    ) -> None:
        """Same as :meth:`sync_observed` but reuses an existing session."""

        await self._sync(
            session=session,
            server_id=server_id,
            bedrock_port=bedrock_port,
            worker_id=worker_id,
            running=running,
        )

    # -- Shared implementation -----------------------------------------------

    async def _sync(
        self,
        *,
        session: AsyncSession,
        server_id: ServerId,
        bedrock_port: int,
        worker_id: WorkerId,
        running: bool,
    ) -> None:
        plugins = await SqlAlchemyPluginRepository(session).list_for_server(server_id)
        if not has_enabled_geyser(plugins):
            _LOG.debug(
                "bedrock tunnel sync skipped: no enabled Geyser plugin",
                extra={"server_id": str(server_id.value)},
            )
            return
        fleet_worker_id = FleetWorkerId(str(worker_id.value))
        fleet_server_id = str(server_id.value)
        if running:
            await self._open(
                worker_id=fleet_worker_id,
                server_id=fleet_server_id,
                bedrock_port=bedrock_port,
            )
        else:
            await self._close(
                worker_id=fleet_worker_id,
                server_id=fleet_server_id,
            )

    async def _open(
        self,
        *,
        worker_id: FleetWorkerId,
        server_id: str,
        bedrock_port: int,
    ) -> None:
        registered = self._relay_registration.current()
        if registered is None:
            _LOG.warning(
                "bedrock tunnel open skipped: no relay registered",
                extra={"server_id": server_id},
            )
            return
        relay_host = registered.endpoint.rsplit(":", 1)[0]
        token = self._bedrock_tunnel_table.open(
            server_id=server_id, bedrock_port=bedrock_port
        )
        try:
            await self._control_plane.dispatch_fire_and_forget(
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
            _LOG.info(
                "bedrock tunnel open skipped: worker not connected",
                extra={"server_id": server_id},
            )

    async def _close(self, *, worker_id: FleetWorkerId, server_id: str) -> None:
        self._bedrock_tunnel_table.close(server_id=server_id)
        try:
            await self._control_plane.dispatch_fire_and_forget(
                worker_id=worker_id,
                server_id=server_id,
                command=CloseBedrockTunnelCommand(),
            )
        except WorkerNotConnectedError:
            _LOG.info(
                "bedrock tunnel close skipped: worker not connected",
                extra={"server_id": server_id},
            )
