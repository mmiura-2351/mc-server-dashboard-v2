"""Per-server count of withdrawn start intents (issue #3739).

A start commits its intent (``desired=running`` on a Worker), hydrates, and only
then sends the start. A stop takes no lock against it, so a stop can commit in
between. The Worker then answers that stop ``SERVER_NOT_FOUND``, since nothing runs
yet, and the stop releases the row. A start sent after that launches an instance
nobody owns. The start's re-read of the row just before it sends (issue #3737)
catches most of these, but not two cases:

- a stop that commits after the re-read and is enqueued before the start;
- a stop that released the row, followed by a newer start that placed it on the
  same Worker again. The re-read then finds ``desired=running`` on this start's
  Worker, as if nothing had happened.

This counter closes both. Every write that withdraws a start's intent bumps the
server's count, synchronously, in the same step as its commit returns. A start
reads the count before its intent exists: before its own compare-and-set, or, for
a re-sent start, before the read that finds the intent. Right before the start is
enqueued, with no await in between, it compares again. If the count moved, the
start is not sent. Otherwise, any stop of this intent bumps later, so it is
enqueued after the start, and the Worker's per-server lane runs it as a real stop.

A bump follows the commit, never precedes it. A start that re-sends an intent
someone else committed reads the count while a stop of that intent may already
be past its own read and waiting on its write. A bump made before that write
would land before the start read the count, and the stop could then be enqueued
ahead of the start unseen. A stop or drain bumps when its commit raises, too: the
commit may have landed (a lost response, or a client disconnect cancelling the
request at that await), and then no stop is sent at all.

What this does not cover is a commit still in flight when a start compares. The
start goes out, and the bump comes after it. If the commit then returns, the stop
is sent behind the start and stops it for real. If the commit lands but raises,
or the write was a drain, no stop follows the start. The reconciler sends one
once the Worker reports the server running. A stop retried before that report
finds ``observed`` still at rest, so it only releases the row, and that release
happens under the instance that is launching (issue #3766).

The writes that bump: a stop's and a drain's ``running -> stopped`` flip, and a
failed start's or placement's revert (``_compensate``, ``_unassign``). A revert
sends no stop, so a start already enqueued when one lands is not caught; a revert
should not touch an intent that is not its own (issue #3741). The later releases
of an already stopped row (the final-snapshot clears, ``redispatch_stop``,
``clear_stale_assignment``) do not bump: the flip that made the row stopped
already did.

The value a start reads also identifies its intent: an unchanged count means no
write has withdrawn the intent since. A later check can use it to decide whether
a start still owns the row.

In-process, deliberately, like ``StopConvergenceGuard``. The API is a single
instance (REQUIREMENTS.md NFR-SCALE-1), and the stops it orders against are sent
from this process. The re-read stays in front of it for what the counter cannot
see: a deleted row, and a write from outside this process.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass
class StopIntentCounter:
    """How many times each server's start intent has been withdrawn in-process."""

    _counts: dict[ServerId, int] = field(default_factory=dict)

    def current(self, server_id: ServerId) -> int:
        return self._counts.get(server_id, 0)

    def bump(self, server_id: ServerId) -> None:
        self._counts[server_id] = self.current(server_id) + 1
