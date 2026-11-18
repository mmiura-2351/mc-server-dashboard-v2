"""Async-SQLAlchemy implementation of the community ``UnitOfWork`` Port.

Opens a session from the factory on ``__aenter__`` and binds the repositories to
it; ``commit`` flushes and commits the transaction, while leaving the block
without committing rolls back (the session is closed either way). This gives use
cases the all-or-nothing transaction the Port promises (DATABASE.md Section 1).

The :class:`ResourceExistenceChecker` Port is bound to a server-table query on the
*same* session, so grant creation's existence check (issue #361) runs inside the
create transaction. Reaching the servers ORM model from the community *adapter*
layer is the same cross-context composition the servers UnitOfWork uses to reach
the community resource-grant adapter, and keeps the community *domain* free of any
other context (ARCHITECTURE.md Section 2.1).

The :class:`UserExistenceChecker` Port is bound the same way, to a query of the
identity ``user`` table on the same session, so a community write on a user -- a
conferral of community ownership, an insert referencing the user -- validates it
inside its own transaction, under the account write lock an account deletion
waits for (issues #3217, #3856).
"""

from __future__ import annotations

import uuid
from types import TracebackType

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mc_server_dashboard_api.community.adapters.integrity import (
    translate_integrity_error,
)
from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyCommunityRepository,
    SqlAlchemyMembershipRepository,
    SqlAlchemyResourceGrantRepository,
    SqlAlchemyRoleRepository,
)
from mc_server_dashboard_api.community.domain.repositories import (
    ResourceExistenceChecker,
    UserExistenceChecker,
)
from mc_server_dashboard_api.community.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    UserId,
)
from mc_server_dashboard_api.core.adapters.account_write_lock import (
    take_account_write_lock,
)
from mc_server_dashboard_api.identity.adapters.models import UserModel
from mc_server_dashboard_api.servers.adapters.models import ServerModel


class SqlAlchemyResourceExistenceChecker(ResourceExistenceChecker):
    """Check resource existence within a community by querying the owning table.

    M1 grants only ``server`` resources, so this queries the ``server`` table for a
    row matching the id *and* community (a server from another community is treated
    as absent, FR-AUTHZ-4). An unknown ``resource_type`` returns ``False`` rather
    than raising: the create use case already rejects unknown types before this is
    reached, so this is a defensive default, not a path the edge can hit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(
        self, community_id: CommunityId, resource_type: str, resource_id: uuid.UUID
    ) -> bool:
        if resource_type != "server":
            return False
        # FOR KEY SHARE holds the server against deletion (the Port's contract)
        # without blocking edits of its non-key columns.
        stmt = (
            select(ServerModel.id)
            .where(
                ServerModel.id == resource_id,
                ServerModel.community_id == community_id.value,
            )
            .with_for_update(read=True, key_share=True)
        )
        return (await self._session.execute(stmt)).first() is not None


class SqlAlchemyUserExistenceChecker(UserExistenceChecker):
    """Check a user exists, under its account write lock (#3217, #3856)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_and_check_exists(self, user_id: UserId) -> bool:
        # Shared: an account deletion holds the lock exclusively from before its
        # ownership check until it commits, so this waits for one in flight,
        # and the read below, a new snapshot, sees whether it deleted the user.
        await take_account_write_lock(self._session, user_id.value, shared=True)
        stmt = select(UserModel.id).where(UserModel.id == user_id.value)
        return (await self._session.execute(stmt)).first() is not None


class SqlAlchemyUnitOfWork(UnitOfWork):
    """:class:`UnitOfWork` adapter over an async-SQLAlchemy session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self._session = self._session_factory()
        self.communities = SqlAlchemyCommunityRepository(self._session)
        self.memberships = SqlAlchemyMembershipRepository(self._session)
        self.roles = SqlAlchemyRoleRepository(self._session)
        self.resource_grants = SqlAlchemyResourceGrantRepository(self._session)
        self.resources = SqlAlchemyResourceExistenceChecker(self._session)
        self.users = SqlAlchemyUserExistenceChecker(self._session)
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

    async def flush(self) -> None:
        assert self._session is not None
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            translate_integrity_error(exc)
            raise

    async def commit(self) -> None:
        assert self._session is not None
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            translate_integrity_error(exc)
            raise

    async def rollback(self) -> None:
        assert self._session is not None
        await self._session.rollback()
