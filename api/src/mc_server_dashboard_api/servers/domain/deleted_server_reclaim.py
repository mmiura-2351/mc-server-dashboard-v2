"""Port: report a committed server delete to the Workers (issue #3679).

A Worker learned that a server it holds scratch for was deleted only when it next
registered (``RegisterAck.unknown_held_server_ids``), so one that stayed connected
kept the deleted server's leftover container and scratch indefinitely. The delete
now reports itself through this Port, and the Workers reclaim at once. The
registration-time reclaim stays the backstop for a Worker the report does not
reach.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class DeletedServerReclaim(abc.ABC):
    """Port: tell the Workers ``server_id`` was deleted, so they reclaim it."""

    @abc.abstractmethod
    async def reclaim(self, server_id: ServerId) -> None:
        """Report the delete to every connected Worker without awaiting them.

        Best-effort: a Worker that is not connected is skipped, since its next
        registration reports the id as deleted.
        """


class NullDeletedServerReclaim(DeletedServerReclaim):
    """No-op :class:`DeletedServerReclaim`: reports nothing.

    The default ``DeleteServer`` carries, so its construction sites that do not
    exercise the control plane need not wire it. The application factory injects
    the fleet-backed adapter.
    """

    async def reclaim(self, server_id: ServerId) -> None:
        return None
