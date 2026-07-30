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

The same Port also answers WHO published that generation, which the held-inventory
refresh (issue #2477) needs to tell "the store's current working set came from this
Worker's scratch" apart from "something else advanced the store past it".

The servers domain/application may not import the storage context (import-linter
contract), so they depend on this narrow Port; the wiring binds it to a
Storage-backed adapter that calls ``Storage.current_generation`` /
``Storage.current_publisher``.
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

    @abc.abstractmethod
    async def current_publisher(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> str | None:
        """Return the id of whoever published the current generation (issue #2477).

        The snapshot scheduler pairs this with :meth:`current_generation` to prove that
        the store's current working set came from THIS Worker's scratch before it
        records the generation as held (#2477): only then is the scratch demonstrably
        at least as fresh as the store. A non-Worker sentinel (an at-rest edit #889, a
        restore #873) or another Worker's id means the store advanced past what this
        Worker holds, so nothing is recorded and the next start hydrates.

        Read AFTER the generation, never before: a publish landing between the two
        reads then pairs the OLDER generation with the NEWER publisher, which can only
        make the check understate or refuse — never claim a generation the Worker does
        not hold. ``None`` when nothing is published or the publish recorded no id.
        """
