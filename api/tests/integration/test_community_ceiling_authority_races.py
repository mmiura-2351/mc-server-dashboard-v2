"""A conferral must not commit after the authority it relied on was revoked (#3241).

The permission ceiling lets an actor confer only what they hold. Each race test
pauses one conferral -- role create, role update, role assignment or grant
create -- after its ceiling check, right before its write, then runs a revocation
of the actor's own authority on another connection: a permission removed from
the actor's role, the role unassigned from the actor, or the actor's grant on
the resource revoked. The ceiling holds the rows it was computed from until the
conferral commits, so the revocation either waits for the conferral (which then
commits first, while the actor still held the permission) or, had it committed
first, the conferral is rejected. A conferral never commits after the revocation.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5), mirroring ``test_community_stale_role_writes.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

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
    SqlAlchemyMembershipRepository,
    SqlAlchemyResourceGrantRepository,
    SqlAlchemyRoleRepository,
)
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.community.adapters.user_directory import (
    IdentityUserDirectory,
)
from mc_server_dashboard_api.community.application.manage_grant import (
    CreateGrant,
    RevokeGrant,
)
from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
    AssignRole,
    UnassignRole,
)
from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    UpdateRole,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import (
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.errors import (
    PermissionCeilingExceededError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    MembershipId,
    Permission,
    ResourceGrantId,
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
_ROLE_MANAGE = Permission("role:manage")
_GRANT_MANAGE = Permission("grant:manage")

# Upper bound on how long a competing transaction may take to settle (commit, or
# block on the paused one's lock) before the test fails instead of resuming.
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


# Where a paused use case stops: at its first write, after a role update's
# locked read of its target, or after the ceiling locked the actor's roles.
_PausePoint = Literal["write", "target-read", "role-locks"]


class _Pause:
    """Holds a use case at its pause point until released."""

    def __init__(self, at: _PausePoint = "write") -> None:
        self.at = at
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self, point: _PausePoint) -> None:
        if point != self.at or self.reached.is_set():
            return
        self.reached.set()
        await self.resume.wait()


class _PausingRoleRepository(SqlAlchemyRoleRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def add(self, role: Role) -> None:
        await self._pause.hold("write")
        await super().add(role)

    async def lock_by_id(self, role_id: RoleId) -> Role | None:
        role = await super().lock_by_id(role_id)
        await self._pause.hold("target-read")
        return role

    async def update(
        self,
        role_id: RoleId,
        *,
        name: RoleName | None = None,
        permissions: set[Permission] | None = None,
        updated_at: dt.datetime,
    ) -> Role:
        await self._pause.hold("write")
        return await super().update(
            role_id, name=name, permissions=permissions, updated_at=updated_at
        )


class _PausingMembershipRepository(SqlAlchemyMembershipRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def lock_role_ids(self, membership_id: MembershipId) -> list[RoleId]:
        role_ids = await super().lock_role_ids(membership_id)
        await self._pause.hold("role-locks")
        return role_ids

    async def assign_role(self, membership_id: MembershipId, role_id: RoleId) -> None:
        await self._pause.hold("write")
        await super().assign_role(membership_id, role_id)


class _PausingResourceGrantRepository(SqlAlchemyResourceGrantRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def add(self, grant: ResourceGrant) -> None:
        await self._pause.hold("write")
        await super().add(grant)


class _PausingUnitOfWork(SqlAlchemyUnitOfWork):
    """Pauses at the given pause's point."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], pause: _Pause
    ) -> None:
        super().__init__(session_factory)
        self._pause = pause

    async def __aenter__(self) -> _PausingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.roles = _PausingRoleRepository(self._session, self._pause)
        self.memberships = _PausingMembershipRepository(self._session, self._pause)
        self.resource_grants = _PausingResourceGrantRepository(
            self._session, self._pause
        )
        return self


class _PidUnitOfWork(SqlAlchemyUnitOfWork):
    """Community unit of work reporting its transaction's backend pid."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pid: asyncio.Future[int],
    ) -> None:
        super().__init__(session_factory)
        self._pid = pid

    async def __aenter__(self) -> _PidUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        value = (
            await self._session.execute(text("SELECT pg_backend_pid()"))
        ).scalar_one()
        self._pid.set_result(value)
        return self


async def _await_settled(
    engine: AsyncEngine, competitor: asyncio.Task[Any], pid: asyncio.Future[int]
) -> None:
    """Return once ``competitor`` has finished or is blocked on a lock.

    Resuming the paused transaction only after this makes the interleaving
    explicit rather than timed.
    """

    query = text(
        "SELECT 1 FROM pg_stat_activity WHERE pid = :pid AND wait_event_type = 'Lock'"
    )
    deadline = asyncio.get_running_loop().time() + _SETTLE_TIMEOUT
    while not competitor.done():
        if pid.done():
            async with engine.connect() as conn:
                if (await conn.execute(query, {"pid": pid.result()})).first():
                    return
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("competitor neither committed nor blocked on a lock")
        await asyncio.sleep(0.02)


@dataclass(frozen=True)
class _World:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    community_id: CommunityId
    owner: UserId
    # Holds role:manage and grant:manage, and server:start either through the
    # Managers role or through a grant on the server.
    manager: UserId
    member: UserId
    managers: RoleId
    # A role without server:start, for the manager to add it to.
    ops: RoleId
    # A role with server:start, for the manager to assign.
    starters: RoleId
    server_id: uuid.UUID
    manager_grant: ResourceGrantId | None


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


async def _insert_server(
    engine: AsyncEngine, server_id: uuid.UUID, community_id: CommunityId
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server "
                "(id, community_id, name, mc_edition, mc_version, server_type, "
                "config, slug, desired_state, observed_state, "
                "created_at, updated_at) VALUES "
                "(:id, :cid, 'survival', 'java', '1.21', 'vanilla', "
                "'{}'::jsonb, :slug, 'stopped', 'stopped', now(), now())"
            ),
            {
                "id": server_id,
                "cid": community_id.value,
                "slug": f"srv-{str(server_id)[:8]}-00",
            },
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


async def _world(engine: AsyncEngine, *, start_via_grant: bool = False) -> _World:
    owner, manager, member = (UserId(uuid.uuid4()) for _ in range(3))
    await _insert_user(engine, owner.value, "owner")
    await _insert_user(engine, manager.value, "manager")
    await _insert_user(engine, member.value, "member")
    factory = create_session_factory(engine)
    community = await ProvisionCommunity(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )(name="guild", owner_user_id=owner)
    for user in (manager, member):
        await AddMember(
            uow=SqlAlchemyUnitOfWork(factory),
            users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
            clock=SystemClock(),
        )(community_id=community.id, user_id=user)
    server_id = uuid.uuid4()
    await _insert_server(engine, server_id, community.id)

    manager_permissions = {_ROLE_MANAGE, _GRANT_MANAGE, _STOP}
    if not start_via_grant:
        manager_permissions.add(_START)
    managers = _role(community.id, "Managers", manager_permissions)
    ops = _role(community.id, "Ops", {_STOP})
    starters = _role(community.id, "Starters", {_START})
    async with SqlAlchemyUnitOfWork(factory) as uow:
        for role in (managers, ops, starters):
            await uow.roles.add(role)
        await uow.flush()
        membership = await uow.memberships.get_by_user_and_community(
            manager, community.id
        )
        assert membership is not None
        await uow.memberships.assign_role(membership.id, managers.id)
        await uow.commit()

    manager_grant = None
    if start_via_grant:
        grant = await CreateGrant(
            uow=SqlAlchemyUnitOfWork(factory), clock=SystemClock()
        )(
            community_id=community.id,
            actor_id=owner,
            user_id=manager,
            resource_type="server",
            resource_id=server_id,
            permissions={_START},
        )
        manager_grant = grant.id
    return _World(
        engine,
        factory,
        community.id,
        owner,
        manager,
        member,
        managers.id,
        ops.id,
        starters.id,
        server_id,
        manager_grant,
    )


# A conferral of server:start by the manager, run on the given unit of work.
_Conferral = Callable[[_World, SqlAlchemyUnitOfWork], Coroutine[Any, Any, object]]
# A revocation of the manager's server:start, run on the given unit of work.
_Revocation = Callable[[_World, SqlAlchemyUnitOfWork], Coroutine[Any, Any, None]]


async def _create_role(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    return await CreateRole(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        actor_id=world.manager,
        name="Launchers",
        permissions={_START},
    )


async def _update_role(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    return await UpdateRole(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        role_id=world.ops,
        actor_id=world.manager,
        permissions={_STOP, _START},
    )


async def _assign_role(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    await AssignRole(uow=uow)(
        community_id=world.community_id,
        user_id=world.member,
        role_id=world.starters,
        actor_id=world.manager,
    )
    return None


async def _create_grant(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    return await CreateGrant(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        actor_id=world.manager,
        user_id=world.member,
        resource_type="server",
        resource_id=world.server_id,
        permissions={_START},
    )


async def _remove_start_from_role(world: _World, uow: SqlAlchemyUnitOfWork) -> None:
    await UpdateRole(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        role_id=world.managers,
        actor_id=world.owner,
        permissions={_ROLE_MANAGE, _GRANT_MANAGE, _STOP},
    )


async def _unassign_role(world: _World, uow: SqlAlchemyUnitOfWork) -> None:
    await UnassignRole(uow=uow)(
        community_id=world.community_id,
        user_id=world.manager,
        role_id=world.managers,
    )


async def _revoke_grant(world: _World, uow: SqlAlchemyUnitOfWork) -> None:
    assert world.manager_grant is not None
    await RevokeGrant(uow=uow)(
        community_id=world.community_id, grant_id=world.manager_grant
    )


async def _race(world: _World, conferral: _Conferral, revocation: _Revocation) -> None:
    pause = _Pause()
    conferring = asyncio.create_task(
        conferral(world, _PausingUnitOfWork(world.factory, pause))
    )
    await pause.reached.wait()

    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    revoking = asyncio.create_task(
        revocation(world, _PidUnitOfWork(world.factory, pid))
    )
    await _await_settled(world.engine, revoking, pid)
    revoked_first = revoking.done()
    pause.resume.set()
    outcome, revoked = await asyncio.gather(
        conferring, revoking, return_exceptions=True
    )

    assert not isinstance(revoked, BaseException)
    if revoked_first:
        # The revocation committed while the conferral was in flight: the
        # conferral must not then commit what the actor no longer holds.
        assert isinstance(outcome, PermissionCeilingExceededError)
    else:
        # The revocation waited: the conferral committed first, while the actor
        # still held the permission, and nothing of it is lost.
        assert not isinstance(outcome, BaseException)


@pytest.mark.parametrize(
    "conferral",
    [_create_role, _update_role, _assign_role, _create_grant],
    ids=["create-role", "update-role", "assign-role", "create-grant"],
)
@pytest.mark.parametrize(
    "revocation",
    [_remove_start_from_role, _unassign_role],
    ids=["role-loses-permission", "role-unassigned"],
)
async def test_conferral_never_commits_after_its_role_authority_is_revoked(
    engine: AsyncEngine, conferral: _Conferral, revocation: _Revocation
) -> None:
    world = await _world(engine)
    await _race(world, conferral, revocation)


async def test_grant_creation_never_commits_after_the_actor_grant_is_revoked(
    engine: AsyncEngine,
) -> None:
    world = await _world(engine, start_via_grant=True)
    await _race(world, _create_grant, _revoke_grant)


async def test_conferral_queued_behind_a_revocation_sees_it(
    engine: AsyncEngine,
) -> None:
    # The revocation holds its lock first; the conferral, arriving meanwhile,
    # must wait for it and evaluate the committed state, not the one it would
    # have read before the revocation committed.
    world = await _world(engine)

    pause = _Pause()
    revoking = asyncio.create_task(
        _remove_start_from_role(world, _PausingUnitOfWork(world.factory, pause))
    )
    await pause.reached.wait()
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    conferring = asyncio.create_task(
        _create_role(world, _PidUnitOfWork(world.factory, pid))
    )
    await _await_settled(world.engine, conferring, pid)
    pause.resume.set()
    revoked, outcome = await asyncio.gather(
        revoking, conferring, return_exceptions=True
    )

    assert not isinstance(revoked, BaseException)
    assert isinstance(outcome, PermissionCeilingExceededError)


@pytest.mark.parametrize("pause_at", ["target-read", "role-locks"])
async def test_managers_editing_each_others_roles_do_not_deadlock(
    engine: AsyncEngine, pause_at: _PausePoint
) -> None:
    # The manager edits the CoManagers role the co-manager holds while the
    # co-manager edits the Managers role the manager holds: each edit locks its
    # target for update and the other's target as its own authority. The first
    # is paused after one of its two lock steps, the second runs into it, and
    # both must then complete: locking the target before the actor's roles, or
    # the actor's roles before the target, would deadlock at one of the two.
    world = await _world(engine)
    co_manager = UserId(uuid.uuid4())
    await _insert_user(engine, co_manager.value, "co_manager")
    await AddMember(
        uow=SqlAlchemyUnitOfWork(world.factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(world.factory)),
        clock=SystemClock(),
    )(community_id=world.community_id, user_id=co_manager)
    co_managers = _role(world.community_id, "CoManagers", {_ROLE_MANAGE, _START})
    async with SqlAlchemyUnitOfWork(world.factory) as uow:
        await uow.roles.add(co_managers)
        await uow.flush()
        membership = await uow.memberships.get_by_user_and_community(
            co_manager, world.community_id
        )
        assert membership is not None
        await uow.memberships.assign_role(membership.id, co_managers.id)
        await uow.commit()

    pause = _Pause(at=pause_at)
    first = asyncio.create_task(
        UpdateRole(uow=_PausingUnitOfWork(world.factory, pause), clock=SystemClock())(
            community_id=world.community_id,
            role_id=co_managers.id,
            actor_id=world.manager,
            permissions={_ROLE_MANAGE, _START, _STOP},
        )
    )
    await pause.reached.wait()
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    second = asyncio.create_task(
        UpdateRole(uow=_PidUnitOfWork(world.factory, pid), clock=SystemClock())(
            community_id=world.community_id,
            role_id=world.managers,
            actor_id=co_manager,
            permissions={_ROLE_MANAGE, _GRANT_MANAGE, _STOP, _START},
        )
    )
    await _await_settled(world.engine, second, pid)
    pause.resume.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    for result in results:
        assert not isinstance(result, BaseException)
