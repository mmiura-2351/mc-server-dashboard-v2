"""The servers-side store-generation seam (the reconciler's view of Storage).

The reconciler's same-worker skip-hydrate decision (``redispatch_start``, issue
#763) compares the generation a Worker reports holding against the AUTHORITATIVE
working-set generation. That authoritative value is Storage's own
``current_generation`` — the counter ``commit_snapshot`` bumps atomically with
publishing the new working set.

The reconciler reads Storage directly (this Port) rather than the
``server.store_generation`` DB mirror so there is no lag window: the snapshot
endpoint publishes (Storage generation advances) and only afterwards mirrors the
new generation onto the DB row in a SEPARATE transaction. If that mirror write
ever fails after a durable publish, the DB would lag Storage — and a Worker
holding the prior generation would then satisfy ``held >= db_generation`` and
wrongly SKIP a hydrate it needs, rolling the world back (#696-class data loss).
Reading the single authoritative source closes that window.

The servers domain/application may not import the storage context (import-linter
contract), so they depend on this narrow Port; the wiring binds it to a
Storage-backed adapter that calls ``Storage.current_generation``.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)


class StoreGenerationReader(abc.ABC):
    """Port: read Storage's authoritative working-set generation (issue #763)."""

    @abc.abstractmethod
    async def current_generation(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> int:
        """Return the authoritative store generation for ``(community, server)``.

        0 when no snapshot has ever been published (no working set to skip a
        hydrate for), matching the Worker's "nothing held" / generation-0 default.
        """
