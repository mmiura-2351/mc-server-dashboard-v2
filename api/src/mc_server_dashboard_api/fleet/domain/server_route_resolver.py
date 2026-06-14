"""The ``ServerRouteResolver`` Port: slug -> routing facts for ResolveJoin.

The relay's ``ResolveJoin`` (RELAY.md Sections 4, 6) maps an incoming player's
slug to a routing decision. The RelayService servicer is a fleet adapter and must
not reach into the servers domain, so it depends on this fleet-domain Port; the
wiring binds it to a servers-backed adapter that does the slug lookup. The Port
speaks in plain values so the fleet domain stays free of the servers domain's
types; the adapter translates at the seam.

The resolver returns only what the decision needs: the server's id (carried into
SessionStart), its display name (the relay synthesizes the stopped-server MOTD
from it), whether it is observed running, and its assigned worker id. The
servicer combines these with the registry's worker-liveness view to decide
NOT_FOUND / STOPPED / TUNNEL — worker liveness is the registry's authority, not
the resolver's.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class ServerRoute:
    """Routing facts about the server a slug resolves to (RELAY.md Section 6)."""

    server_id: str
    display_name: str
    # True when the server's cached observed state is ``running`` (the only state
    # the relay tunnels to; everything else is treated as STOPPED, RELAY.md
    # Section 7).
    is_running: bool
    # The assigned worker's id, or ``None`` when nothing is placed. A running
    # server with no assigned worker is treated as STOPPED.
    assigned_worker_id: str | None


class ServerRouteResolver(abc.ABC):
    """Port: resolve a relay slug to the server's routing facts."""

    @abc.abstractmethod
    async def resolve_slug(self, slug: str) -> ServerRoute | None:
        """Return the :class:`ServerRoute` for ``slug``, or ``None`` if unknown.

        ``None`` maps to a NOT_FOUND decision (the relay drops the connection
        silently). A known server resolves to a route the servicer then classifies
        as STOPPED or TUNNEL.
        """
