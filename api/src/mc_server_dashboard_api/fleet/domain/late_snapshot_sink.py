"""The ``LateSnapshotResultSink`` Port: late final-snapshot results into state.

A final-snapshot dispatch that TIMED OUT abandons its pending future, leaving the
stop wedged at (stopped, stopped, assigned) and held for the reconciler's
stale-stop arm to clear once grace lapses (issue #847). When the Worker later
reports the snapshot's outcome — a ``TRANSFER_FAILED`` once the worker's transfer
bound aborts the upload (#874/#890), or a late SUCCESS when the publish landed but
the response was slow — that ``CommandResult`` arrives unmatched (no pending
future). Rather than drop it and wait out grace, the control-plane state hands it
to this Port so the held assignment is released minutes earlier (issue #891).

The servicer is a fleet adapter and must not reach into the servers domain, so it
depends on this fleet-domain Port (mirroring :class:`ServerStateSink`); the wiring
binds it to a servers-backed adapter that runs the guarded clear.

The Port speaks in plain values (worker id string, server id string) so the fleet
domain stays free of the servers domain's types; the adapter translates them at
the seam.
"""

from __future__ import annotations

import abc


class LateSnapshotResultSink(abc.ABC):
    """Port: release a held assignment on a late final-snapshot result."""

    @abc.abstractmethod
    async def clear_held_assignment_on_late_snapshot(
        self, *, server_id: str, worker_id: str, succeeded: bool
    ) -> None:
        """Release ``server_id``'s held assignment on a late snapshot result (#891).

        Invoked only for an unmatched ``CommandResult`` that the control-plane
        state recognises as a snapshot it dispatched to ``worker_id`` whose future
        was already abandoned (a dispatch timeout). The adapter runs the same
        guarded clear the final-snapshot path uses: it matches only a still
        desired=stopped row still assigned to ``worker_id`` — so a report from a
        worker that no longer owns the server clears nothing (the #789 ownership
        guard, enforced by the guarded UPDATE), and a row a racing start re-placed
        is left untouched.

        ``succeeded`` is the result's outcome: ``False`` for ``TRANSFER_FAILED``
        (the upload is dead — the held progression since the last periodic snapshot
        is lost, logged loud), ``True`` for a late SUCCESS (the publish landed; the
        clear is the same release the on-time success would have run).
        """
