"""The servers-side store-generation seam (the reconciler's view of Storage).

The reconciler's same-worker skip-hydrate decision (``redispatch_start``, issue
#763) compares the generation a Worker reports holding against the AUTHORITATIVE
working-set generation. That authoritative value is Storage's own
``current_generation`` — the counter ``commit_snapshot`` bumps atomically with
publishing the new working set.

The reconciler reads Storage directly (this Port) — the single authoritative
source — so there is no lag window. The generation advances atomically with the
working set it names on ``commit_snapshot``, so a Worker holding the prior
generation can never satisfy ``held >= store`` and wrongly SKIP a hydrate it
needs, which would roll the world back (#696-class data loss).

The servers domain/application may not import the storage context (import-linter
contract), so they depend on this narrow Port; the wiring binds it to a
Storage-backed adapter that calls ``Storage.current_generation``.
"""

from __future__ import annotations

import abc

from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
    WorkerId,
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

        Waits for a publish that has passed its assignment fence (issue #3552).
        Read after the caller's assignment commits, it therefore sees every
        publish that fence lets through, so a late publish of the previous
        Worker's final snapshot cannot land after a skip-hydrate decision.
        """

    @abc.abstractmethod
    async def uploads_settled(
        self, *, community_id: CommunityId, server_id: ServerId, worker_id: WorkerId
    ) -> bool:
        """Wait for the server's snapshot uploads from ``worker_id`` (issue #3950).

        Returns once none of them is still being received or committed, and so
        once each one that lands has published. False if one still is when the
        wait's bound runs out. A hydrate of ``worker_id`` sent after a True
        therefore serves at least what those uploads publish. The wait holds no
        lock an upload needs.
        """
