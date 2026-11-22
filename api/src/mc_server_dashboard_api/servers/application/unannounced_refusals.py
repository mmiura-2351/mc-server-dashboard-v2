"""Per-server record of a final-snapshot refusal written unannounced (issue #4152).

A stop whose final snapshot the Worker refuses ``BUSY`` or ``UNAVAILABLE`` writes
the refusal on the row (``final_snapshot_refusal``, issue #3697), and the unit of
work publishes the ``{final_snapshot_refusal}`` status frame once the COMMIT
answers. A COMMIT that lands with its answer lost (the #4062 shape) fails the
write as an outage, so the row holds the refusal and no frame was published. The
next refused attempt then reads the refusal as recorded and writes nothing, so
live subscribers would miss the hold until a reload.

So that write is recorded here, and the next refused attempt of the server, in
time or late, writes the refusal as a retry (``retry=True``). A retry stages its
frame even when the row already holds the value, so one frame is published. The
entry is forgotten once a write of the refusal is confirmed, and when the server
is released, since a release publishes its own frame and clears the refusal.

Only a failed COMMIT is recorded. A write that fails before its COMMIT was sent
changed nothing, and the next attempt reads the row as it is. A COMMIT that
failed without landing is recorded all the same, since its caller cannot tell
the two apart, and costs at most one frame that repeats the value the row
already held.

Held in memory, like ``unrecorded_stops.py``. An API restart loses the entry, and
the frame is then not published. That is accepted: the hold is the row, so a
reload shows it.

Size: one entry per server at most, kept only until the server's next confirmed
write of a refusal or its release.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass
class UnannouncedRefusals:
    """The servers whose last write of a refusal may have landed unannounced."""

    _servers: set[ServerId] = field(default_factory=set)

    def record(self, server_id: ServerId) -> None:
        """Record that a COMMIT of ``server_id``'s refusal failed after it was sent."""

        self._servers.add(server_id)

    def forget(self, server_id: ServerId) -> None:
        """Drop the entry: the refusal was announced, or the server released."""

        self._servers.discard(server_id)

    def pending(self, server_id: ServerId) -> bool:
        """Whether the next write of ``server_id``'s refusal must be a retry."""

        return server_id in self._servers
