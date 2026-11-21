"""A store ownership whose verdict a test sets directly (issue #3870).

Satisfies the versions and servers ``StoreOwnership`` ports, which ask one
question of the store owner marker: why a destructive pass must not run, if it
must not. It also stands in for the app's real marker (``tests/conftest.py``),
so it takes the claim and the blocking read the storage adapters make.

Beside it, the database side of the real claim (issue #3886): one whose rows a
test sets, and one the claim must not ask at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from mc_server_dashboard_api.storage.adapters.ownership import StoreDatabase

_FOREIGN = (
    "the store owner marker mcsd-owner.json names installation "
    "00000000-0000-4000-8000-000000000002, but this database is installation "
    "00000000-0000-4000-8000-000000000001"
)


class FakeStoreOwnership:
    """``refusal`` is ``None`` (the store is ours) unless a test sets one."""

    def __init__(self, refusal: str | None = None) -> None:
        self.refusal_reason = refusal

    @classmethod
    def foreign(cls) -> FakeStoreOwnership:
        """A store whose marker names another installation."""

        return cls(_FOREIGN)

    async def claim(
        self, installation_id: uuid.UUID, *, database: StoreDatabase
    ) -> None:
        pass

    async def refusal(self) -> str | None:
        return self.refusal_reason

    def refusal_blocking(self) -> str | None:
        return self.refusal_reason


class FakeStoreDatabase:
    """The database side of the startup claim, with rows a test sets (#3886).

    ``rows`` answers whether any row names a stored object; ``communities`` and
    ``resource_packs`` are the ids that are rows. Every question asked is
    recorded in ``asked``, so a test can pin which ones the claim asks.
    """

    def __init__(
        self,
        *,
        rows: bool = False,
        communities: Iterable[uuid.UUID] = (),
        resource_packs: Iterable[uuid.UUID] = (),
    ) -> None:
        self.rows = rows
        self.communities = frozenset(communities)
        self.resource_packs = frozenset(resource_packs)
        self.asked: list[tuple[str, ...]] = []

    async def names_stored_objects(self) -> bool:
        self.asked.append(("names_stored_objects",))
        return self.rows

    async def has(
        self,
        *,
        communities: frozenset[uuid.UUID],
        resource_packs: frozenset[uuid.UUID],
    ) -> bool:
        self.asked.append(
            ("has", *sorted(map(str, communities)), *sorted(map(str, resource_packs)))
        )
        return communities <= self.communities and resource_packs <= self.resource_packs


class UnaskedStoreDatabase:
    """A database the claim must not ask anything: the store has a marker."""

    async def names_stored_objects(self) -> bool:
        raise AssertionError("asked the database about a store that has a marker")

    async def has(
        self,
        *,
        communities: frozenset[uuid.UUID],
        resource_packs: frozenset[uuid.UUID],
    ) -> bool:
        raise AssertionError("asked the database about a store that has a marker")
