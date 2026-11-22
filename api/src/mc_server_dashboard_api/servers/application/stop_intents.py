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
commit may have landed (a lost response, or the task cancelled at that await,
which a client disconnect does not do on the shipped server stack unless the
handler reads the request body, issue #3950), and then no stop is sent at all.
The cost is a commit that truly failed: the intent is still running, but an
in-flight start of it is refused and waits for the reconciler's re-send.

What this does not hold back is a withdrawal that sends no stop and lands after a
start compares: a stop commit that was still in flight and then lands but
raises, or any drain, including one that begins after the start was sent. Nor a
re-send that read the count after a withdrawal's bump but the row before its
flip was readable, as a commit that raises, cancelled, before its flip is
visible to other sessions would allow: the bump then comes first, the pre-write
shape argued against above. Either way the start goes out, and nothing follows
it. (A stop commit that lands after the compare and returns is fine: its stop is
sent behind the start and is a real stop.) The reconciler sends a stop once the
Worker reports the server running.

A stop convergence that runs before that report (an operator stop of a stopped
intent runs one at once, issue #3491) must not release the row under the
launching instance on the strength of a cached ``observed`` (issue #3766). So
this also notes each start as it is enqueued, and each stop the Worker ran
(answered OK or ``SERVER_NOT_FOUND``), which stopped every start queued ahead of
it. While a start noted here has no such stop behind it, ``clear_stale_assignment``
sends a stop instead of releasing. A stop the Worker refused does not count: the
start it queued behind may still be launching. No start can be queued between a
stop and its answer, because the stop's own bump comes before it.

``redispatch_stop`` and ``clear_stale_assignment`` bump too, before they send or
release, because the write that stopped the row may not have bumped yet (a drain
whose commit has not returned) or may have bumped too early (above). A start
that compares after that bump is not sent. One that compared before it is
queued ahead of the stop, or noted as sent before the release checks. So no
release comes before a bump of the intent it releases, and a start of a newer
intent never reads the count a failed start of the old one read (#3791's
interaction, issue #3766). How asyncpg orders a cancelled commit's raise and its
flip then no longer matters to them: their own bump covers both orders.

The writes that bump: a stop's and a drain's ``running -> stopped`` flip, a
failed start's or placement's revert of its own intent (``_compensate``,
``_unassign``), which also bumps when its commit raises, and the two stop
convergences above. A revert sends no stop, so a re-sent start of that same
intent already enqueued when the revert lands is not caught. The final-snapshot
clears do not bump: the flow that took the snapshot already did.

The value a start reads also identifies its intent: an unchanged count means no
write in this process has withdrawn the intent since. A failed start's revert
checks it, with the row, so that it undoes only its own intent and never a newer
start's (issue #3741). ``start_intent_lock.py`` keeps a newer intent from landing
between that check and the revert's write.

In-process, deliberately, like ``StopConvergenceGuard``. The API is a single
instance (REQUIREMENTS.md NFR-SCALE-1), and the stops it orders against are sent
from this process. The re-read stays in front of it for what the counter cannot
see: a deleted row, and a write from outside this process.

So the record of sent starts is empty after an API restart. A start the previous
process sent, whose instance has not reported yet, is not in it, and the clear
releases as it did before issue #3766. Its compare of ``observed_at`` with the
row it read is then the only cover, and only for a report that lands before the
release. A restart's relaunch is not noted either: in-process, the entry the
server's own start left still covers it. Issue #3854 tracks both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass
class StopIntentCounter:
    """How many times each server's start intent has been withdrawn in-process.

    Also which servers have had a start sent that no stop has stopped yet.
    """

    # Neither drops a deleted server's entry: each holds at most one per server
    # this process has seen.
    _counts: dict[ServerId, int] = field(default_factory=dict)
    _unstopped_starts: set[ServerId] = field(default_factory=set)

    def current(self, server_id: ServerId) -> int:
        return self._counts.get(server_id, 0)

    def bump(self, server_id: ServerId) -> None:
        self._counts[server_id] = self.current(server_id) + 1

    def start_sent(self, server_id: ServerId) -> None:
        """Note a start enqueued for ``server_id``, as it is enqueued."""

        self._unstopped_starts.add(server_id)

    def stop_ran(self, server_id: ServerId) -> None:
        """Note a stop the Worker ran: it stopped every start sent before it."""

        self._unstopped_starts.discard(server_id)

    def start_may_be_running(self, server_id: ServerId) -> bool:
        """Whether a start was sent for ``server_id`` and no stop has run since."""

        return server_id in self._unstopped_starts
