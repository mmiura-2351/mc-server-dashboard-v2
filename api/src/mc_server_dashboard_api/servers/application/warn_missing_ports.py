"""Startup WARN for legacy NULL-game_port servers (issue #310).

A server row created before port tracking (#243) carries ``game_port IS NULL``.
Such rows are excluded from the deployment-wide taken-port set, so
auto-assignment can hand a new server the host port a legacy server already binds
(via its ``server.properties``) — a guaranteed host-port collision at launch.

:class:`WarnLegacyMissingPorts` makes that gap discoverable: run once on API
startup, it WARN-logs the count and ids of every server with no tracked port, so
an operator can backfill them (the manual SQL in DEPLOYMENT.md Section 7, or the
update-port API). It is read-only and informational — it never mutates a row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork

_LOG = logging.getLogger(__name__)


@dataclass
class WarnLegacyMissingPorts:
    """WARN-log servers with no tracked game port so operators can backfill them."""

    uow: UnitOfWork

    async def __call__(self) -> int:
        async with self.uow as uow:
            ids = await uow.servers.list_ids_missing_game_port()
        if ids:
            _LOG.warning(
                "servers with no tracked game_port (legacy/imported rows that "
                "predate port tracking) are invisible to port auto-assignment and "
                "can collide on the host port; backfill them per DEPLOYMENT.md "
                "Section 6",
                extra={
                    "count": len(ids),
                    "server_ids": [str(server_id.value) for server_id in ids],
                },
            )
        return len(ids)
