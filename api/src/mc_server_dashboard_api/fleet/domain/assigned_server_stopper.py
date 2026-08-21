"""The ``AssignedServerStopper`` Port: stop what a drained Worker is running.

Draining a Worker has two halves (FR-WRK-5): the registry flag that excludes it
from placement, which this context owns, and marking every server assigned to it
``desired=stopped``, which the servers context owns. This Port is the fleet-side
name for that second half, so ``SetWorkerDrain`` states the intent in fleet terms
and the servers procedure is bound to it at the adapter layer
(``fleet/adapters/assigned_server_stopper.py``) rather than imported inward
(ARCHITECTURE.md Section 2.2, issue #2578).

The Port carries no unit of work and no clock: the transaction belongs entirely to
the side that owns the server records.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId


class AssignedServerStopper(abc.ABC):
    """Port: record the stop intent for the servers a Worker currently holds."""

    @abc.abstractmethod
    async def stop_assigned(self, worker_id: WorkerId) -> list[str]:
        """Mark every server assigned to ``worker_id`` ``desired=stopped``.

        Returns the ids of the servers this call actually moved out of running,
        already committed. A server another stop had already moved is skipped and
        NOT reported, so the caller sheds placement load for exactly the servers
        that left it. An id no server can be assigned under, and an unknown Worker,
        stop nothing and return an empty list.
        """
