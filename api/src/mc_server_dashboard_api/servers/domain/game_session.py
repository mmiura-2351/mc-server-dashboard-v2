"""Entity for a recorded game session (RELAY.md Sections 8, 14; issue #957).

A :class:`GameSession` is one accepted **login** session as seen by the relay:
the player's source IP, the slug they joined on, and the identity *claimed* in
Login Start (pre-authentication — see RELAY.md Section 8). Status pings are not
recorded. The relay mints the ``id`` (a UUID) and reports start/end events
through ``ReportSessions``; the API persists them idempotently keyed on that id.

Sessions live in the servers context: they hang off the ``Server`` aggregate
(``server_id`` FK, ``ON DELETE CASCADE``) and the sessions read endpoint is
scoped ``(community_id, server_id)`` like the other server sub-resources, so a
separate context would only re-import that scope across a boundary.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


class GameSessionSource(enum.Enum):
    """The relay ingress path a session was accepted on (issue #1912).

    Lets the history distinguish a Bedrock flow-session from a Java
    login-session whose claimed identity was unparseable. ``UNSPECIFIED`` is the
    legacy value: a pre-migration row, or a session an older relay reported
    before the ``SessionStart.source`` field existed.
    """

    UNSPECIFIED = "unspecified"
    JAVA = "java"
    BEDROCK = "bedrock"


@dataclass(frozen=True)
class GameSession:
    """One recorded login session (DATABASE.md / RELAY.md Section 14).

    ``username`` / ``player_uuid`` are the *claimed* Login Start values and may be
    absent. ``ended_at`` is ``None`` while the session is open (or until orphan
    healing on the next relay ``Register`` closes it).
    """

    id: uuid.UUID
    # server_id / hostname / player_ip / started_at are present in steady state
    # but may be ``None`` on an end-before-start placeholder not yet reconciled by
    # its start (RELAY.md Section 6 idempotency).
    server_id: ServerId | None
    hostname: str | None
    player_ip: str | None
    username: str | None
    player_uuid: uuid.UUID | None
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    # The relay ingress path (issue #1912); UNSPECIFIED for legacy rows whose
    # ``source`` column is NULL.
    source: GameSessionSource = GameSessionSource.UNSPECIFIED
