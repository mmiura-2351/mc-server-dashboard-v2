"""A store ownership whose verdict a test sets directly (issue #3870).

Satisfies the versions and servers ``StoreOwnership`` ports, which ask one
question of the store owner marker: why a destructive pass must not run, if it
must not. It also stands in for the app's real marker (``tests/conftest.py``),
so it takes the claim and the blocking read the storage adapters make.
"""

from __future__ import annotations

import uuid

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

    async def claim(self, installation_id: uuid.UUID) -> None:
        pass

    async def refusal(self) -> str | None:
        return self.refusal_reason

    def refusal_blocking(self) -> str | None:
        return self.refusal_reason
