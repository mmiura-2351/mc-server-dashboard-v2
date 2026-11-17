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
request at that await), and then no stop is sent at all. The cost is a commit
that truly failed: the intent is still running, but an in-flight start of it is
refused and waits for the reconciler's re-send.

What this does not cover is a withdrawal that sends no stop and lands after a
start compares: a stop commit that was still in flight and then lands but
raises, or any drain, including one that begins after the start was sent. The
start goes out, and nothing follows it. (A stop commit that lands after the
compare and returns is fine: its stop is sent behind the start and is a real
stop.) The reconciler sends a stop once the Worker reports the server running.
A stop retried before that report finds ``observed`` still at rest, so it only
releases the row, under the instance that is launching (issue #3766). Also open,
and recorded there: whether a cancelled commit can raise before its flip is
visible to other sessions. The bump would then come first, the pre-write shape
argued against above, and a re-send that read the count in that gap would not be
held back.

The writes that bump: a stop's and a drain's ``running -> stopped`` flip, and a
failed start's or placement's revert of its own intent (``_compensate``,
``_unassign``), which also bumps when its commit raises. A revert sends no stop,
so a re-sent start of that same intent already enqueued when the revert lands is
not caught. The later releases of an
already stopped row (the final-snapshot clears, ``redispatch_stop``,
``clear_stale_assignment``) do not bump: the flip that made the row stopped
already did.

The value a start reads also identifies its intent: an unchanged count means no
write in this process has withdrawn the intent since. A failed start's revert
checks it, with the row, so that it undoes only its own intent and never a newer
start's (issue #3741). ``start_intent_lock.py`` keeps a newer intent from landing
between that check and the revert's write.

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
