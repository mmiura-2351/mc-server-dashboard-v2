"""Fleet-backed adapter for the servers :class:`BedrockTunnelCredentials` seam.

Binds the delete-time credential eviction to the process-local fleet
``BedrockTunnelTable`` (issue #1544). This is an adapter-layer composition
across contexts (mirroring :mod:`server_state_sink`, which drives the same
table on the other side); the servers *domain*/*application* never import fleet
(import-linter).
"""

from __future__ import annotations

from mc_server_dashboard_api.fleet.adapters.relay_state import BedrockTunnelTable
from mc_server_dashboard_api.servers.domain.bedrock_tunnel import (
    BedrockTunnelCredentials,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class FleetBedrockTunnelCredentials(BedrockTunnelCredentials):
    """Evict a server's credential from the fleet ``BedrockTunnelTable``."""

    def __init__(self, table: BedrockTunnelTable) -> None:
        self._table = table

    def close(self, server_id: ServerId) -> None:
        self._table.close(server_id=str(server_id.value))
