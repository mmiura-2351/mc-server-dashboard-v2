"""The ``StoreOwnership`` seam: whether the JAR pool's store is this database's.

The JAR-pool GC deletes every pooled JAR no server row of this database
references, so on a store another database also uses it would delete that
database's JARs. The store's owner marker settles whose store it is (STORAGE.md
Section 4.3, issue #3870); the wiring binds this Port to it at the edge.
"""

from __future__ import annotations

from typing import Protocol


class StoreOwnership(Protocol):
    """Port: may a destructive pass run against the store right now?"""

    async def refusal(self) -> str | None:
        """Why it must not, or ``None`` when the store belongs to this database."""
        ...
