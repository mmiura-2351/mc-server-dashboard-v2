"""Persistence Port for game-session records (RELAY.md Section 14; issue #957).

The read endpoint and the retention prune loop depend on this Port; an
async-SQLAlchemy adapter implements it on the unit-of-work's session. Ingestion
(insert-on-start, close-on-end, orphan healing on relay ``Register``) is a
separate concern driven by the relay servicer through a fleet-domain ``SessionSink``
Port, so it is not part of this read/prune surface.
"""

from __future__ import annotations

import abc
import datetime as dt

from mc_server_dashboard_api.servers.domain.game_session import GameSession
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class GameSessionRepository(abc.ABC):
    """Port: read and prune :class:`GameSession` rows."""

    @abc.abstractmethod
    async def list_for_server(
        self, server_id: ServerId, *, limit: int, offset: int
    ) -> list[GameSession]:
        """Return a server's sessions newest-first (the ``session:read`` listing).

        Ordered by ``started_at`` descending (ties broken by ``id``), windowed by
        ``limit``/``offset``. Community scoping is enforced by the caller, which
        loads the (community-checked) server first; this is keyed by ``server_id``
        only, backed by the ``(server_id, started_at)`` index.
        """

    @abc.abstractmethod
    async def delete_started_before(self, cutoff: dt.datetime) -> int:
        """Delete sessions whose ``started_at`` is strictly older than ``cutoff``.

        End-only placeholder rows (``started_at IS NULL`` — the start was lost to
        the relay's drop-oldest cap or the server was deleted before a late start)
        are pruned by their ``ended_at`` instead, so they do not live forever.

        Returns the number of rows deleted. The retention prune loop computes
        ``cutoff = now - relay.session_retention_days`` and calls this each tick;
        rows also cascade away when their server is deleted.
        """
