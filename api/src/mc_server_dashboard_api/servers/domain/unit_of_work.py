"""The servers-context ``UnitOfWork`` Port: transactional grouping.

A use case opens a unit of work as an async context manager, performs its writes
through the repositories it exposes, and calls :meth:`commit` to make them
durable; leaving the block without committing rolls back (DATABASE.md Section 1).
This is what lets the server delete and its resource-grant sweep (Section 10) be
applied atomically: both ``servers`` and ``resource_grants`` run on the one
transaction this unit of work owns.
"""

from __future__ import annotations

import abc
from types import TracebackType

from mc_server_dashboard_api.servers.domain.backup_repository import (
    BackupRepository,
)
from mc_server_dashboard_api.servers.domain.game_session_repository import (
    GameSessionRepository,
)
from mc_server_dashboard_api.servers.domain.group_repository import GroupRepository
from mc_server_dashboard_api.servers.domain.plugin_repository import PluginRepository
from mc_server_dashboard_api.servers.domain.repositories import (
    ResourceGrantSweeper,
    ServerRepository,
)


class UnitOfWork(abc.ABC):
    """Port: an atomic transaction exposing the servers repositories."""

    servers: ServerRepository
    resource_grants: ResourceGrantSweeper
    backups: BackupRepository
    groups: GroupRepository
    game_sessions: GameSessionRepository
    plugins: PluginRepository

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
