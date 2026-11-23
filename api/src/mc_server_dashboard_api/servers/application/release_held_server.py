"""Release a held server without its final snapshot (issue #4055, #3697).

A stop keeps its Worker assigned until the final snapshot settles (#847). When
that Worker refuses the snapshot ``BUSY`` or ``UNAVAILABLE``, the row records
the refusal in ``final_snapshot_refusal`` and stays held, with no bound, so no
start elsewhere and no at-rest write can lose the progression the snapshot still
owes (owner decision on #3701). This is the operator's explicit way out, a
holder of ``server:stop`` only: it releases the row as the stop would have, but
without the snapshot.

It releases nothing else. A row held for another reason (a snapshot not
answered yet, a session that ended, a disconnected Worker's grace) records no
refusal and may still get its snapshot, so it is refused. So is a row whose
stop convergence or final snapshot is in flight: releasing under an upload that
may still publish is the #847 race. A refused row whose retry was then left
unanswered keeps its refusal, and stays releasable once nothing is in flight
(issue #4111): that retry did not settle the snapshot it is held for.

It touches only the database (PM ruling 3(a) on #3697) and sends the Worker no
command. What it leaves behind:

- The Worker keeps its scratch, so the progression since the last snapshot is
  lost once the server starts on another Worker, or once a write to its files
  advances the store generation, after which even that Worker hydrates over it.
- A container of the server may still run on that Worker until the Worker next
  starts the server, reclaims it or boots, so a start elsewhere can run the
  world twice in the meantime.
- It cannot be undone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.stop_convergence import (
    StopConvergenceGuard,
)
from mc_server_dashboard_api.servers.application.unannounced_refusals import (
    UnannouncedRefusals,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidLifecycleTransitionError,
    LifecycleTransitionConflictError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReleaseHeldServer:
    """Release a server held by a refused final snapshot, without it (#4055).

    ``stop_convergence`` is the process-wide exclusion every stop convergence
    holds (``stop_convergence.py``), so the release refuses while one of them,
    or a final snapshot of the server, is in flight. ``unannounced_refusals``
    is the stops' process-wide record of a refusal written unannounced
    (``unannounced_refusals.py``), which the release forgets.
    """

    uow: UnitOfWork
    stop_convergence: StopConvergenceGuard
    unannounced_refusals: UnannouncedRefusals

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> Server:
        """Release the server and return it as the release left it.

        Raises ``InvalidLifecycleTransitionError`` unless the server is at rest
        under a stop intent, still assigned, with a refusal recorded; and
        ``LifecycleTransitionConflictError``, writing nothing, while a stop
        convergence or a final snapshot of the server is in flight, or when a
        Worker report lands between the read and the release.
        """

        with self.stop_convergence.claim(server_id):
            async with self.uow:
                server = await self.uow.servers.get_by_id(server_id)
                if server is None or server.community_id != community_id:
                    raise ServerNotFoundError(str(server_id.value))
                worker_id = server.assigned_worker_id
                refusal = server.final_snapshot_refusal
                # At rest under the stop intent, and held by a refusal: the
                # release reads the row the refusal was written on.
                if worker_id is None or refusal is None or not server.is_at_rest():
                    raise InvalidLifecycleTransitionError(str(server_id.value))
                # Guarded on the Worker and on the observed_at read above
                # (issue #3766): a report since then leaves the row as it is.
                released = await self.uow.servers.clear_stale_assignment(
                    server_id, worker_id, observed_at=server.observed_at
                )
                if not released:
                    raise LifecycleTransitionConflictError(str(server_id.value))
                await self.uow.commit()
        # Its frame clears the refusal, so no later refusal is owed one for a
        # write whose COMMIT failed (issue #4152).
        self.unannounced_refusals.forget(server_id)
        # The release wrote these, and nothing else (issue #4056).
        server.assigned_worker_id = None
        server.awaiting_hydrate = False
        server.final_snapshot_refusal = None
        _LOG.warning(
            "released server %s from worker %s without its final snapshot, "
            "which the worker refused (%s): the progression since the last "
            "snapshot is lost once the server starts on another worker or its "
            "files change, and a container may still run on that worker",
            server_id.value,
            worker_id.value,
            refusal.value,
        )
        return server
