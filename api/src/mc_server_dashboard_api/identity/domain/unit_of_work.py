"""The ``UnitOfWork`` Port: transactional grouping over the repositories.

A use case opens a unit of work as an async context manager, performs its writes
through the repositories it exposes, and calls :meth:`commit` to make them
durable; leaving the block without committing rolls back (DATABASE.md Section 1).
This is what lets multi-row invariants (e.g. the FR-MEM-3 cascades) be applied
atomically.
"""

from __future__ import annotations

import abc
from types import TracebackType

from mc_server_dashboard_api.identity.domain.repositories import (
    RefreshTokenRepository,
    UserRepository,
)


class UnitOfWork(abc.ABC):
    """Port: an atomic transaction exposing the identity repositories."""

    users: UserRepository
    refresh_tokens: RefreshTokenRepository

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
    async def commit(self) -> None:
        """Make the staged changes durable."""

    @abc.abstractmethod
    async def rollback(self) -> None:
        """Discard the staged changes."""
