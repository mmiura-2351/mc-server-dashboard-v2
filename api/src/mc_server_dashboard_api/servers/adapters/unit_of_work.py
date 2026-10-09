"""Async-SQLAlchemy implementation of the servers ``UnitOfWork`` Port.

Opens a session from the factory on ``__aenter__`` and binds the repositories to
it; ``commit`` commits the transaction, while leaving the block without
committing rolls back (the session is closed either way). This gives use cases
the all-or-nothing transaction the Port promises (DATABASE.md Section 1).

A commit also publishes the status events of the observed-state writes it made
durable, and a rollback discards them (issue #3212): the servers repository
stages one per changed row, so a use case that writes ``observed_state`` cannot
forget the live frame, and no frame precedes its commit.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import TracebackType

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.fleet.domain.real_time_events import RealTimeEvents
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
    publish_status_events,
)
from mc_server_dashboard_api.servers.adapters.resource_pack_repository import (
    SqlAlchemyResourcePackRepository,
)
from mc_server_dashboard_api.servers.adapters.schedule_repository import (
    SqlAlchemyScheduleRepository,
    SqlAlchemyScheduleRunRepository,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork


class SqlAlchemyUnitOfWork(UnitOfWork):
    """:class:`UnitOfWork` adapter over an async-SQLAlchemy session."""

    servers: SqlAlchemyServerRepository

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        real_time_events: RealTimeEvents | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        # The bus a commit publishes its observed-state writes on. Wired for
        # every unit of work that can write ``observed_state``; ``None`` (a
        # tool with no live subscribers) drops the events.
        self._real_time_events = real_time_events

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.servers = SqlAlchemyServerRepository(self._session)
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
        publish_status_events(self.servers, self._real_time_events)

    async def rollback(self) -> None:
        assert self._session is not None
        await self._session.rollback()
        self.servers.take_staged_status_events()

    @contextlib.asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        # A SAVEPOINT, so a refused write rolls back to it instead of
        # deactivating the whole transaction (issue #2612). Leaving the block
        # normally releases it -- flushing what the body staged, which is also
        # what puts the refusal on the statement the body owns rather than on
        # whichever later autoflush would otherwise have run it.
        assert self._session is not None
        async with self._session.begin_nested():
            yield
