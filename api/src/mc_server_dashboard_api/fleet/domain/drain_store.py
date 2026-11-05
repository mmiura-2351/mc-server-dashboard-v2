"""The ``WorkerDrainStore`` Port: the durable record of drain intent (FR-WRK-5).

Draining is the operator's decommissioning intent, and it must outlive an API
restart the way a server's ``desired_state`` does (issue #3535). The registry
stays in memory (DATABASE.md Section 7); this Port persists only which Worker ids
are drained, so the wiring can seed the registry with them at startup and a
Worker re-registering after a restart comes back DRAINING.

Like ``AssignedServerStopper``, the Port carries no unit of work: each call owns
its transaction and has committed when it returns.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId


class WorkerDrainStore(abc.ABC):
    """Port: record, clear, and list the drained Worker ids."""

    @abc.abstractmethod
    async def drain(self, worker_id: WorkerId) -> None:
        """Record ``worker_id`` as drained; idempotent, committed on return."""

    @abc.abstractmethod
    async def undrain(self, worker_id: WorkerId) -> None:
        """Clear the drain record of ``worker_id``; idempotent, committed on return."""

    @abc.abstractmethod
    async def drained(self) -> frozenset[WorkerId]:
        """Return every drained Worker id."""
