"""Per-server record of a finished stop whose database write failed (issue #3441).

When the Worker confirms a stop, the API writes the result in steps: observed
``stopped``, the final snapshot, then the release of the held assignment. A late
result for an abandoned final snapshot is written the same way. If one of these
writes fails (canonically a database outage), the row stays diverged and still
assigned, and nothing on it says the stop finished. It is then identical to a stop
that may never have been dispatched, so the reconciler waits the full
``grace_seconds`` (660 s) before it converges the row.

Two floors make up that grace on the stop side, and this record satisfies both:

* #930, ``> stop_timeout_seconds``: never re-send a stop whose first dispatch may
  still be running. Here the Worker answered the stop, so that round trip is over.
* #847, ``> snapshot_timeout_seconds``: never release an assignment while a final
  snapshot may still be uploading. The record is made only when no upload can be
  live: before this flow dispatched its final snapshot, after a final snapshot
  that settled, or on the late result of an abandoned one.

So the reconciler can take the short ``refused_stop_grace_seconds`` for either
stop-side action (``redispatch_stop`` or ``clear_stale_assignment``), as it does for
a refused stop (``stop_dispatch_refusals.py``, issue #2478). The owed action itself
is unchanged. As there, the record only counts while it is the most recent thing
known about the row: a newer intent commit or Worker report supersedes it.

:meth:`forget` runs before every stop and every final-snapshot dispatch, so a
recorded entry only ever follows the latest dispatch. A dispatch that then times
out leaves no entry, and the full grace covers it, because the command may still be
running.

Held in memory, deliberately, for the same reasons as the refusal record. The
knowledge is "*this process* saw the stop finish", and an API restart loses it. The
full grace then applies again, which is the conservative answer, and no persisted
marker can go stale across a restart. A write that failed because the database is
down could not have persisted a marker anyway.

Size: one timestamp per server at most. An entry that is never forgotten (the row
converged by some path that dispatched nothing) is unreachable, because the
reconciler only looks up rows it is converging and server ids are never reused,
the same bound as the refusal record.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass
class UnrecordedStops:
    """When each server's finished stop last failed to be written, if it did."""

    _recorded_at: dict[ServerId, dt.datetime] = field(default_factory=dict)

    def forget(self, server_id: ServerId) -> None:
        """Drop the entry, because a new stop or final snapshot is being dispatched."""

        self._recorded_at.pop(server_id, None)

    def record(self, server_id: ServerId, *, at: dt.datetime) -> None:
        """Record that a finished stop of ``server_id`` failed to be written at ``at``.

        Callers record only when nothing is in flight for the server: the Worker
        answered the stop, and no final snapshot of it can still be uploading.
        """

        self._recorded_at[server_id] = at

    def recorded_at(self, server_id: ServerId) -> dt.datetime | None:
        """Return when this server's finished stop failed to be written, if it did."""

        return self._recorded_at.get(server_id)
