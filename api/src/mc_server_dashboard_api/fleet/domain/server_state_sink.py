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
    ) -> bool:
        """Cache the worker-reported observed state for ``server_id`` (FR-SRV-4).

        Returns True when the write was applied, False when it was dropped
        (unknown server, ownership guard, or monotonic guard). The caller uses
        the flag to gate downstream side-effects such as real-time event
        publishing (issue #1957).

        ``worker_id`` is the reporting worker; the adapter applies the write
        only when it is the server's assigned worker, so a stale or misrouted
        report from a worker that no longer owns the server cannot overwrite
        authoritative state (defense-in-depth).
        """

    @abc.abstractmethod
    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        """Set observed=unknown for every server assigned to ``worker_id``.

        Invoked when the worker disconnects: its servers' observed state is no
        longer trustworthy (FR-WRK-4).
        """

    @abc.abstractmethod
    async def existing_server_ids(self, *, server_ids: list[str]) -> set[str]:
        """Return the subset of ``server_ids`` that still exist in the store.

        Used by the RegisterAck to compute which held servers the Worker
        advertised are no longer known (deleted while the scratch was live,
        issue #924). The Worker reclaims the orphaned scratch for each unknown id.
        """

    @abc.abstractmethod
    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        """Return ``worker_id``'s desired=running assignments, id -> declared memory.

        Maps each assigned server id to its declared ``memory_limit_mb`` (0 = unset,
        #843). The registry resets assignments on (re)registration; the lifecycle
        layer rebuilds them from this authoritative tally so both placement load and
        committed memory are correct after a reconnect (epic #7 reconciliation
        obligation). The ids (not just a count) let the registry reconcile against
        reserved-but-uncommitted placements: a reservation whose server is already in
        this map is dropped (the commit landed and is counted here), while one not yet
        in the map stays pending so its confirm still counts (#778).
        """
