"""Async-SQLAlchemy implementation of the servers ``UnitOfWork`` Port.

Opens a session from the factory on ``__aenter__`` and binds the repositories to
it; ``commit`` commits the transaction, while leaving the block without
committing rolls back (the session is closed either way). This gives use cases
the all-or-nothing transaction the Port promises (DATABASE.md Section 1) — needed
for the server-delete-plus-grant-sweep (Section 10).

The grant-sweep half binds the servers :class:`ResourceGrantSweeper` Port to the
*community* context's resource-grant adapter on the **same** session, so the
server delete and the grant sweep are one transaction. Reusing the community
adapter here (an adapter-layer, cross-context composition) keeps the sweep logic
in the one place that owns ``resource_grant`` while honouring the rule that the
servers *domain* imports no other context (ARCHITECTURE.md Section 2.1).
"""

from __future__ import annotations

import uuid
from types import TracebackType

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyResourceGrantRepository,
)
from mc_server_dashboard_api.servers.adapters.backup_repository import (
    SqlAlchemyBackupRepository,
)
from mc_server_dashboard_api.servers.adapters.game_session_repository import (
    SqlAlchemyGameSessionRepository,
)
from mc_server_dashboard_api.servers.adapters.group_repository import (
    SqlAlchemyGroupRepository,
)
from mc_server_dashboard_api.servers.adapters.integrity import (
    translate_integrity_error,
)
from mc_server_dashboard_api.servers.adapters.plugin_repository import (
    SqlAlchemyPluginRepository,
)
from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.adapters.resource_pack_repository import (
    SqlAlchemyResourcePackRepository,
)
from mc_server_dashboard_api.servers.adapters.schedule_repository import (
    SqlAlchemyScheduleRepository,
    SqlAlchemyScheduleRunRepository,
)
from mc_server_dashboard_api.servers.domain.repositories import ResourceGrantSweeper
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork


class _ResourceGrantSweeperAdapter(ResourceGrantSweeper):
    """Bind the servers grant-sweep Port to the community resource-grant adapter.

    A thin wrapper so the community repository (which implements the *community*
    Port) satisfies the servers ``ResourceGrantSweeper`` Port without the servers
    domain ever referencing the community domain. Both share the one session.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._grants = SqlAlchemyResourceGrantRepository(session)

    async def delete_for_resource(
        self, resource_type: str, resource_id: uuid.UUID
    ) -> None:
        await self._grants.delete_for_resource(resource_type, resource_id)


class SqlAlchemyUnitOfWork(UnitOfWork):
    """:class:`UnitOfWork` adapter over an async-SQLAlchemy session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.servers = SqlAlchemyServerRepository(self._session)
        self.resource_grants = _ResourceGrantSweeperAdapter(self._session)
        self.backups = SqlAlchemyBackupRepository(self._session)
        self.groups = SqlAlchemyGroupRepository(self._session)
        self.game_sessions = SqlAlchemyGameSessionRepository(self._session)
        self.plugins = SqlAlchemyPluginRepository(self._session)
        self.resource_packs = SqlAlchemyResourcePackRepository(self._session)
        self.schedules = SqlAlchemyScheduleRepository(self._session)
        self.schedule_runs = SqlAlchemyScheduleRunRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._session is not None
        try:
            await self.rollback()
        finally:
            await self._session.close()
            self._session = None

    async def commit(self) -> None:
        assert self._session is not None
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            # Translate a known unique violation (an INSERT racer flushing at
            # commit) into the typed domain error; see adapters/integrity.py.
            translate_integrity_error(exc)
            raise

    async def rollback(self) -> None:
        assert self._session is not None
        await self._session.rollback()
