"""One-shot Bedrock port sweep on gate flip (issue #1588).

When ``relay.bedrock_enabled`` transitions from off to on (operator changes the
config and restarts the API), servers that already have an installed, enabled
Geyser plugin but no ``bedrock_port`` must receive one. Without this sweep those
servers are not Bedrock-joinable until Geyser is reinstalled.

:class:`SweepBedrockPorts` runs once at startup when the Bedrock deployment gate
is on. It is idempotent: servers that already carry a ``bedrock_port`` are
skipped, and ``UNIQUE(bedrock_port)`` backstops a concurrent racer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.errors import PortRangeExhaustedError
from mc_server_dashboard_api.servers.domain.ports import (
    PortRange,
    pick_lowest_free_port,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork

_LOG = logging.getLogger(__name__)


@dataclass
class SweepBedrockPorts:
    """Allocate ``bedrock_port`` for every Geyser-enabled server missing one."""

    uow: UnitOfWork
    port_range: PortRange
    clock: Clock

    async def __call__(self) -> int:
        async with self.uow as uow:
            all_servers = await uow.servers.list_all()
            candidates = [s for s in all_servers if s.bedrock_port is None]
            if not candidates:
                return 0
            candidate_ids = [s.id for s in candidates]
            geyser_ids = await uow.plugins.enabled_geyser_server_ids(candidate_ids)
            if not geyser_ids:
                return 0
            taken = await uow.servers.list_bedrock_ports()
            now = self.clock.now()
            count = 0
            for server in candidates:
                if server.id not in geyser_ids:
                    continue
                try:
                    port = pick_lowest_free_port(self.port_range, taken=taken)
                except PortRangeExhaustedError:
                    _LOG.warning(
                        "bedrock gate-flip sweep: port range exhausted after "
                        "allocating %d server(s); remaining servers skipped",
                        count,
                    )
                    break
                server.bedrock_port = port
                server.updated_at = now
                await uow.servers.update(server)
                taken.add(port)
                count += 1
            await uow.commit()
        if count:
            _LOG.info(
                "bedrock gate-flip sweep: allocated bedrock_port for %d server(s)",
                count,
            )
        return count
