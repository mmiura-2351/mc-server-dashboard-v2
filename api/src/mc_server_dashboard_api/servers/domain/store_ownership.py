"""The ``StoreOwnership`` seam: whether the object store is this database's.

The plugin-cache and resource-pack GCs delete every stored blob no row of this
database names, so in a bucket another database also uses they would delete that
database's blobs. The bucket's owner marker settles whose bucket it is
(STORAGE.md Section 4.3, issue #3870); the wiring binds this Port to it at the
edge.
"""

from __future__ import annotations

from typing import Protocol


class StoreOwnership(Protocol):
    """Port: may a destructive pass run against the store right now?"""

    async def refusal(self) -> str | None:
        """Why it must not, or ``None`` when the store belongs to this database."""
        ...
