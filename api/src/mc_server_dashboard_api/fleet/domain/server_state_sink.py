"""The ``ServerStateSink`` Port: the control plane's write-back into server state.

The control-plane event path observes runtime facts about *servers* — a worker
reports a ``StatusChange``, a worker disconnects, a worker re-registers — that
must be reflected in the authoritative ``server`` records (FR-SRV-4, FR-WRK-4).
The servicer is a fleet adapter and must not reach into the servers domain, so it
depends on this fleet-domain Port; the wiring binds it to a servers-backed
adapter that does the DB writes.

The Port speaks in plain values (worker id string, server id string, observed
state string) so the fleet domain stays free of the servers domain's types; the
adapter translates them at the seam.
"""

from __future__ import annotations

import abc


class ServerStateSink(abc.ABC):
    """Port: reconcile authoritative server state from control-plane events."""

    @abc.abstractmethod
    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str
    ) -> None:
        """Cache the worker-reported observed state for ``server_id`` (FR-SRV-4).

        A report for an unknown server is ignored (the worker may know a server
        the API has since deleted). ``worker_id`` is the reporting worker; the
        adapter applies the write only when it is the server's assigned worker, so
        a stale or misrouted report from a worker that no longer owns the server
        cannot overwrite authoritative state (defense-in-depth).
        """

    @abc.abstractmethod
    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        """Set observed=unknown for every server assigned to ``worker_id``.

        Invoked when the worker disconnects: its servers' observed state is no
        longer trustworthy (FR-WRK-4).
        """

    @abc.abstractmethod
    async def count_running_assignments(self, *, worker_id: str) -> int:
        """Return how many servers are assigned to ``worker_id`` with desired=running.

        The registry resets assignment counts on (re)registration; the lifecycle
        layer rebuilds the count from this authoritative tally so placement load
        is correct after a reconnect (epic #7 reconciliation obligation).
        """
