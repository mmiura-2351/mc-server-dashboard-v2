"""The ``SessionSink`` Port: the relay's game-session write-back (RELAY.md 6, 8).

The relay reports session lifecycle events (``ReportSessions``) and its active
set on (re)connect (``Register``); both must land in the ``game_session`` records.
The RelayService servicer is a fleet adapter and must not reach into the servers
domain, so it depends on this fleet-domain Port; the wiring binds it to a
servers-backed adapter that does the DB writes.

The Port speaks in plain values (id strings, ip/host strings, datetimes) so the
fleet domain stays free of the servers domain's types; the adapter translates at
the seam. All three methods are idempotent so the relay's at-least-once retries
(after transient API errors) and crash-recovery re-registrations are safe.
"""

from __future__ import annotations

import abc
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionStart:
    """A relay ``SessionStart`` event (RELAY.md Section 6), in plain values.

    ``username`` / ``player_uuid`` are the *claimed* Login Start values and may be
    absent. ``session_id`` and ``server_id`` are UUID strings the relay supplied;
    a start for an unknown ``server_id`` is dropped by the adapter (the server may
    have been deleted).
    """

    session_id: str
    server_id: str
    hostname: str
    player_ip: str
    username: str | None
    player_uuid: str | None
    started_at: dt.datetime
    # The relay ingress path (``"java"`` / ``"bedrock"``), or ``None`` when the
    # relay did not report one — an older relay predating the field, stored as
    # the legacy/unspecified source (issue #1912).
    source: str | None = None


class SessionSink(abc.ABC):
    """Port: persist relay-reported game sessions."""

    @abc.abstractmethod
    async def record_start(self, start: SessionStart) -> None:
        """Insert a session row, or ignore if ``session_id`` already exists.

        Idempotent: a duplicate start (a retry) is a no-op. If an end for the same
        id arrived first (end-before-start), the existing row's ``started_at`` and
        claimed fields are filled in without clearing the recorded ``ended_at``.
        """

    @abc.abstractmethod
    async def record_end(self, *, session_id: str, ended_at: dt.datetime) -> None:
        """Set ``ended_at`` on the session, upserting if the start has not arrived.

        Idempotent: a duplicate end leaves the recorded ``ended_at`` unchanged. An
        end whose start has not yet arrived creates a placeholder row carrying only
        the id and ``ended_at`` (filled in when the start arrives).
        """

    @abc.abstractmethod
    async def close_absent(
        self, *, active_session_ids: Sequence[str], ended_at: dt.datetime
    ) -> int:
        """Close every open session whose id is *not* in ``active_session_ids``.

        Sets ``ended_at`` on each open (``ended_at IS NULL``) row absent from the
        relay's active set: orphan healing for sessions a relay crash dropped
        (RELAY.md Sections 6, 10). Returns the number of rows closed.
        """
