"""A stale role edit must not undo a concurrently committed one (#3215).

Each test pauses one :class:`UpdateRole` right after it reads the role, lets a
second :class:`UpdateRole` run on another connection, then resumes the paused one.
The role writer persists only the columns the request supplied, so a rename
cannot restore a permission set it read before a permission edit committed, and a
permission edit cannot restore a name it read before a rename committed. A
permission edit additionally reads the role under a row lock, so the permission
ceiling is evaluated against the set its write replaces: a ``role:manage`` holder
without ``server:delete`` cannot carry a concurrently removed ``server:delete``
back in by resubmitting a set computed from the obsolete read.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5), mirroring ``test_community_roles_grants.py``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mc_server_dashboard_api.community.adapters.clock import SystemClock
from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyRoleRepository,
)
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.community.adapters.user_directory import (
    IdentityUserDirectory,
)
from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
)
from mc_server_dashboard_api.community.application.manage_role import UpdateRole
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import Role
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
    RoleId,
    RoleName,
    UserId,
)
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as IdentityUnitOfWork,
)
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_START = Permission("server:start")
_STOP = Permission("server:stop")
_DELETE = Permission("server:delete")

# Upper bound on how long a competing edit may take to settle (commit, or block
# on the paused edit's row lock) before the test fails instead of resuming.
_SETTLE_TIMEOUT = 10.0


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


class _Pause:
    """Holds a use case right after its first role read until released."""

    def __init__(self) -> None:
        self.read_done = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        if self.read_done.is_set():
            return
        self.read_done.set()
        await self.resume.wait()


class _PausingRoleRepository(SqlAlchemyRoleRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def get_by_id(self, role_id: RoleId) -> Role | None:
        role = await super().get_by_id(role_id)
        await self._pause.hold()
        return role

    async def lock_by_id(self, role_id: RoleId) -> Role | None:
        role = await super().lock_by_id(role_id)
        await self._pause.hold()
        return role


class _PausingUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], pause: _Pause
    ) -> None:
        super().__init__(session_factory)
        self._pause = pause

    async def __aenter__(self) -> _PausingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.roles = _PausingRoleRepository(self._session, self._pause)
        return self


class _PidRecordingUnitOfWork(SqlAlchemyUnitOfWork):
    """Records the PostgreSQL backend pid its transaction runs on."""

    pid: int | None = None

    async def __aenter__(self) -> _PidRecordingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.pid = (
            await self._session.execute(text("SELECT pg_backend_pid()"))
        ).scalar_one()
        return self


async def _await_settled(
    engine: AsyncEngine, competitor: asyncio.Task[Role], uow: _PidRecordingUnitOfWork
) -> None:
    """Return once ``competitor`` has finished or is blocked on a row lock.

    Resuming the paused edit only after this makes the interleaving explicit
    rather than timed: an implementation that does not serialize the two edits
    has let the competitor commit, and one that does has it waiting on the lock.
    """

    query = text(
        "SELECT 1 FROM pg_stat_activity WHERE pid = :pid AND wait_event_type = 'Lock'"
    )
    deadline = asyncio.get_running_loop().time() + _SETTLE_TIMEOUT
    while not competitor.done():
        if uow.pid is not None:
            async with engine.connect() as conn:
                if (await conn.execute(query, {"pid": uow.pid})).first():
                    return
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("competing edit neither committed nor blocked on the lock")
        await asyncio.sleep(0.02)


@dataclass(frozen=True)
class _World:
    factory: async_sessionmaker[AsyncSession]
    community_id: CommunityId
    owner: UserId
    # Holds role:manage but not server:delete.
    manager: UserId
    target: RoleId


async def _insert_user(engine: AsyncEngine, user_id: uuid.UUID, username: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                'INSERT INTO "user" '
                "(id, username, email, password_hash, is_platform_admin, "
                "created_at, updated_at) VALUES "
                "(:id, :username, :email, 'h', false, now(), now())"
            ),
            {"id": user_id, "username": username, "email": f"{username}@e.com"},
        )


def _role(community_id: CommunityId, name: str, permissions: set[Permission]) -> Role:
    now = SystemClock().now()
    return Role(
        id=RoleId.new(),
        community_id=community_id,
        name=RoleName(name),
        permissions=permissions,
        created_at=now,
        updated_at=now,
    )


async def _world(engine: AsyncEngine) -> _World:
    owner = UserId(uuid.uuid4())
    manager = UserId(uuid.uuid4())
    await _insert_user(engine, owner.value, "owner")
    await _insert_user(engine, manager.value, "manager")
    factory = create_session_factory(engine)
    community = await ProvisionCommunity(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )(name="guild", owner_user_id=owner)
    await AddMember(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )(community_id=community.id, user_id=manager)

    managers = _role(
        community.id, "Managers", {Permission("role:manage"), _START, _STOP}
    )
    target = _role(community.id, "Ops", {_START, _DELETE})
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.roles.add(managers)
        await uow.roles.add(target)
        await uow.flush()
        membership = await uow.memberships.get_by_user_and_community(
            manager, community.id
        )
        assert membership is not None
        await uow.memberships.assign_role(membership.id, managers.id)
        await uow.commit()
    return _World(factory, community.id, owner, manager, target.id)


def _update(uow: SqlAlchemyUnitOfWork) -> UpdateRole:
    return UpdateRole(uow=uow, clock=SystemClock())


async def _load(world: _World) -> Role:
    async with SqlAlchemyUnitOfWork(world.factory) as uow:
        role = await uow.roles.get_by_id(world.target)
    assert role is not None
    return role


async def test_stale_rename_preserves_concurrent_permission_removal(
    engine: AsyncEngine,
) -> None:
    # The issue's reproduction: a rename by a manager without server:delete reads
    # the role, the owner removes server:delete, then the rename writes.
    world = await _world(engine)

    pause = _Pause()
    rename = asyncio.create_task(
        _update(_PausingUnitOfWork(world.factory, pause))(
            community_id=world.community_id,
            role_id=world.target,
            actor_id=world.manager,
            name="Operators",
        )
    )
    await pause.read_done.wait()
    await _update(SqlAlchemyUnitOfWork(world.factory))(
        community_id=world.community_id,
        role_id=world.target,
        actor_id=world.owner,
        permissions={_START},
    )
    pause.resume.set()
    returned = await rename

    persisted = await _load(world)
    assert persisted.permissions == {_START}
    assert persisted.name == RoleName("Operators")
    # The response reflects the persisted row, not the stale read.
    assert returned.permissions == {_START}


async def test_stale_permission_edit_preserves_concurrent_rename(
    engine: AsyncEngine,
) -> None:
    world = await _world(engine)

    pause = _Pause()
    removal = asyncio.create_task(
        _update(_PausingUnitOfWork(world.factory, pause))(
            community_id=world.community_id,
            role_id=world.target,
            actor_id=world.owner,
            permissions={_START},
        )
    )
    await pause.read_done.wait()
    competitor = _PidRecordingUnitOfWork(world.factory)
    rename = asyncio.create_task(
        _update(competitor)(
            community_id=world.community_id,
            role_id=world.target,
            actor_id=world.manager,
            name="Operators",
        )
    )
    await _await_settled(engine, rename, competitor)
    pause.resume.set()
    await asyncio.gather(removal, rename)

    persisted = await _load(world)
    assert persisted.permissions == {_START}
    assert persisted.name == RoleName("Operators")


async def test_stale_permission_edit_cannot_reintroduce_a_removed_permission(
    engine: AsyncEngine,
) -> None:
    # The manager reads {start, delete} and submits it plus server:stop, which
    # they hold. Against that read only server:stop is newly conferred, so the
    # ceiling passes. Had the owner's removal of server:delete committed in
    # between, the write would confer server:delete -- a permission the manager
    # lacks -- without the ceiling ever seeing it.
    world = await _world(engine)

    pause = _Pause()
    stale = asyncio.create_task(
        _update(_PausingUnitOfWork(world.factory, pause))(
            community_id=world.community_id,
            role_id=world.target,
            actor_id=world.manager,
            permissions={_START, _DELETE, _STOP},
        )
    )
    await pause.read_done.wait()
    competitor = _PidRecordingUnitOfWork(world.factory)
    removal = asyncio.create_task(
        _update(competitor)(
            community_id=world.community_id,
            role_id=world.target,
            actor_id=world.owner,
            permissions={_START},
        )
    )
    await _await_settled(engine, removal, competitor)
    pause.resume.set()
    await asyncio.gather(stale, removal)

    # The removal serializes behind the manager's edit and replaces its result.
    persisted = await _load(world)
    assert persisted.permissions == {_START}
