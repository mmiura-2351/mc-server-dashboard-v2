"""The ``LateSnapshotResultSink`` Port: late final-snapshot results into state.

A final-snapshot dispatch that TIMED OUT abandons its pending future, leaving the
stop wedged at (stopped, stopped, assigned) and held for the reconciler's
stale-stop arm to clear once grace lapses (issue #847). When the Worker later
reports the snapshot's outcome — a failure, canonically a ``TRANSFER_FAILED`` once
the worker's transfer bound aborts the upload (#874/#890) but potentially any
failure, or a late SUCCESS when the publish landed but the response was slow —
that ``CommandResult`` arrives unmatched (no pending future). Rather than drop it
and wait out grace, the control-plane state hands it — the Worker's failure detail
included (#2766) — to this Port so the held assignment is released minutes earlier
(issue #891).

The servicer is a fleet adapter and must not reach into the servers domain, so it
depends on this fleet-domain Port (mirroring :class:`ServerStateSink`); the wiring
binds it to a servers-backed adapter that runs the guarded clear.

The Port speaks in plain values (worker id string, server id string) so the fleet
domain stays free of the servers domain's types; the adapter translates them at
the seam.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.fleet.domain.control_plane import CommandResultCode


class LateSnapshotResultSink(abc.ABC):
    """Port: release a held assignment on a late final-snapshot result."""

    @abc.abstractmethod
    async def clear_held_assignment_on_late_snapshot(
        self,
        *,
        server_id: str,
        worker_id: str,
        code: CommandResultCode,
        message: str | None,
        retry: bool = False,
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

        ``code`` is the result's code: ``OK`` for a late SUCCESS (the publish
        landed; the clear is the same release the on-time success would have run),
        any other for a failure, which leaves publication unconfirmed and means a
        later cross-worker re-placement may lose progression since the last
        periodic snapshot (#845/#847), logged loud. A late ``BUSY`` or
        ``UNAVAILABLE`` releases nothing: the Worker refused the snapshot and still
        holds the working set it owes, so the row stays held and records the
        refusal (issue #3697).

        ``message`` is the Worker's failure detail, the only text that names WHY the
        snapshot failed (a misconfigured data-plane URL, an auth error, a storage
        outage — issue #2766), falling back to the result's error code when the
        Worker sent no text; ``None`` on a late SUCCESS, which has no failure to
        explain. It is not optional to pass: dropping it left the consumer asserting
        a cause it never observed. The caller resolves it, so a failed result always
        carries something to log — mirroring the ``message or status.value`` every
        other failed-command log in the API renders.

        ``retry`` marks a retry of a result whose earlier attempt failed. That
        attempt may have committed the release with its answer lost, so a retry
        that finds the row already released under the stopped intent announces
        and logs the release, as that attempt would have (issue #4062).
        """
