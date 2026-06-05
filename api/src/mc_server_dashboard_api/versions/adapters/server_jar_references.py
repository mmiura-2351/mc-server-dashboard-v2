"""Bind the versions :class:`LiveJarReferences` seam to the servers repository.

An adapter-layer composition across bounded contexts (mirroring the versions
``JarPool`` -> storage ``JarStore`` binding): the versions *domain*/*application*
never import the servers context, so this adapter — bound only in the wiring — reads
the resolved-JAR content key off every server row's ``config`` blob
(``resolved_jar_sha256``, the field ``StartServer`` records) and returns the set of
distinct keys. That set is the JAR-pool GC's live reference set (#293).

A bounded scan of all server rows. The GC runs daily (or on a platform-admin
trigger), so loading the aggregates through the servers ``UnitOfWork`` — the same
read path the data-plane resolved-JAR lookup uses — is acceptable and keeps the
servers-side query logic in the one place that owns it.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import JAR_KEY_CONFIG_FIELD
from mc_server_dashboard_api.versions.domain.jar_references import LiveJarReferences


@dataclass(frozen=True)
class ServerJarReferences(LiveJarReferences):
    """Read live resolved-JAR keys from server rows (the GC reference set)."""

    uow: UnitOfWork

    async def live(self) -> set[str]:
        async with self.uow:
            servers = await self.uow.servers.list_all()
        keys: set[str] = set()
        for server in servers:
            value = server.config.get(JAR_KEY_CONFIG_FIELD)
            if isinstance(value, str):
                keys.add(value)
        return keys
