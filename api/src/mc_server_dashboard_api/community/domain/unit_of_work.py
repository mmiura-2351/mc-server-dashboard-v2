"""The community-context ``UnitOfWork`` Port: transactional grouping.

A use case opens a unit of work as an async context manager, performs its writes
through the repositories it exposes, and calls :meth:`commit` to make them
durable; leaving the block without committing rolls back (DATABASE.md Section 1).
This is what lets the FR-MEM-3 grant-cleanup-plus-membership-delete (Section 10)
be applied atomically.
"""

from __future__ import annotations

import abc
from types import TracebackType

from mc_server_dashboard_api.community.domain.repositories import (
    CommunityRepository,
    MembershipRepository,
    ResourceGrantRepository,
    RoleRepository,
)


class UnitOfWork(abc.ABC):
    """Port: an atomic transaction exposing the community repositories."""

    communities: CommunityRepository
    memberships: MembershipRepository
    roles: RoleRepository
    resource_grants: ResourceGrantRepository

    @abc.abstractmethod
    async def __aenter__(self) -> UnitOfWork:
        """Begin the transaction and bind the repositories."""

    @abc.abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Roll back if the block is left without an explicit commit."""

    @abc.abstractmethod
    async def flush(self) -> None:
        """Flush staged INSERTs so later rows can reference them by FK.

        ``membership_role`` references ``role`` and ``membership``, but these are
        modelled by plain FK columns (no ORM relationship), so the unit of work
        cannot infer the insert order on its own. A use case that stages a row,
        then a row that FKs it, flushes between the two — without committing — so
        the foreign-key target exists when the dependent insert runs.
        """

    @abc.abstractmethod
    async def commit(self) -> None:
        """Make the staged changes durable."""

    @abc.abstractmethod
    async def rollback(self) -> None:
        """Discard the staged changes."""
