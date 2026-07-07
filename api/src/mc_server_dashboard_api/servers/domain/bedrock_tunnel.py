"""The servers-context ``BedrockTunnelCredentials`` Port: forget a tunnel token.

The API keeps a per-server Bedrock relay tunnel credential in memory (the fleet
``BedrockTunnelTable``) that the relay validates a Worker's QUIC dial-out
against (issue #1544). Deleting a server must forget its credential, or the
entry lingers as a small leak — and, because ``validate`` matches on
``(server_id, bedrock_port, token)`` alone, a stale entry would keep answering
valid for a server that no longer exists.

``DeleteServer`` is the authoritative delete, so it evicts the credential
through this narrow Port. The servers domain/application may not import the
fleet context (import-linter), so it depends on this abstraction; the wiring
binds it to a fleet-backed adapter that clears the real table.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.value_objects import ServerId, WorkerId


class BedrockTunnelSync(abc.ABC):
    """Port: open/close a Bedrock tunnel to match observed state (issue #1602).

    The lifecycle's INVALID_STATE convergence writes observed=running through
    the repository directly, bypassing the sink's ``_sync_bedrock_tunnel``.
    This Port lets the application layer invoke the tunnel open/close without
    importing the adapters or fleet context (import-linter).
    """

    @abc.abstractmethod
    async def sync_observed(
        self,
        *,
        server_id: ServerId,
        worker_id: WorkerId,
        bedrock_port: int | None,
        running: bool,
    ) -> None:
        """Open or close the tunnel for ``server_id`` based on ``running``."""


class NullBedrockTunnelSync(BedrockTunnelSync):
    """No-op :class:`BedrockTunnelSync` for callers that never exercise the path."""

    async def sync_observed(
        self,
        *,
        server_id: ServerId,
        worker_id: WorkerId,
        bedrock_port: int | None,
        running: bool,
    ) -> None:
        return None


class BedrockTunnelCredentials(abc.ABC):
    """Port: forget a server's in-memory Bedrock tunnel credential (issue #1544)."""

    @abc.abstractmethod
    def close(self, server_id: ServerId) -> None:
        """Forget ``server_id``'s tunnel credential; idempotent if none is held."""


class NullBedrockTunnelCredentials(BedrockTunnelCredentials):
    """No-op :class:`BedrockTunnelCredentials`: forgets nothing.

    The default ``DeleteServer`` carries so its construction sites (and the unit
    tests that do not exercise the relay path) need not wire the real table. The
    application factory injects the fleet-backed adapter when the relay is wired.
    """

    def close(self, server_id: ServerId) -> None:
        return None
