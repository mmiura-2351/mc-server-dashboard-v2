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
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
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
    RemoveMember,
    UnassignRole,
)
from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    DeleteRole,
    UpdateRole,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import (
    Membership,
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.errors import (
    GrantTargetNotMemberError,
    MembershipNotFoundError,
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
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import DeleteServer
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.integration.races import await_settled, race_database
from tests.servers.fakes import FakeBackupArchiveStore

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_START = Permission("server:start")
_STOP = Permission("server:stop")
_DELETE = Permission("server:delete")
_ROLE_MANAGE = Permission("role:manage")
_GRANT_MANAGE = Permission("grant:manage")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


# Where a paused use case stops: at its first write, after a role update's
# locked read of its target, after the ceiling's ordered role pass, after the
# ceiling locked the actor's assignments, or after a member removal's last-owner
# guard locked the Owner assignments.
_PausePoint = Literal["write", "target-read", "role-pass", "role-locks", "owner-guard"]


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

    async def lock_by_ids(
        self, role_ids: Sequence[RoleId], *, for_update: RoleId | None = None
    ) -> list[Role]:
        roles = await super().lock_by_ids(role_ids, for_update=for_update)
        await self._pause.hold("role-pass")
        return roles

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

    async def lock_role_ids(
        self, membership_id: MembershipId, role_ids: Sequence[RoleId]
    ) -> list[RoleId]:
        role_ids = await super().lock_role_ids(membership_id, role_ids)
        await self._pause.hold("role-locks")
        return role_ids

    async def lock_owner_role_holders(
        self, community_id: CommunityId, role_id: RoleId
    ) -> list[MembershipId]:
        holders = await super().lock_owner_role_holders(community_id, role_id)
        await self._pause.hold("owner-guard")
        return holders

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
    """Pauses at the given pause's point; reports its backend pid if asked."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pause: _Pause,
        pid: asyncio.Future[int] | None = None,
    ) -> None:
        super().__init__(session_factory)
        self._pause = pause
        self._pid = pid

    async def __aenter__(self) -> _PausingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        if self._pid is not None:
            self._pid.set_result(
                (
                    await self._session.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
            )
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
    await await_settled(
        engine, competitor, lambda: pid.result() if pid.done() else None
    )


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


async def _add_delete_to_starters(world: _World, uow: SqlAlchemyUnitOfWork) -> None:
    # Not a revocation but the same race from the other side: the role being
    # assigned gains a permission the assigning manager lacks.
    await UpdateRole(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        role_id=world.starters,
        actor_id=world.owner,
        permissions={_START, _DELETE},
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


async def test_assignment_never_commits_a_permission_its_role_gained_meanwhile(
    engine: AsyncEngine,
) -> None:
    # The ceiling is evaluated against the role's permission set as the
    # assignment commits it: had the owner's addition of server:delete committed
    # in between, the manager -- who lacks server:delete -- would confer it.
    world = await _world(engine)
    await _race(world, _assign_role, _add_delete_to_starters)


async def _add_co_owner(world: _World) -> UserId:
    co_owner = UserId(uuid.uuid4())
    await _insert_user(world.engine, co_owner.value, "co_owner")
    await AddMember(
        uow=SqlAlchemyUnitOfWork(world.factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(world.factory)),
        clock=SystemClock(),
    )(community_id=world.community_id, user_id=co_owner)
    async with SqlAlchemyUnitOfWork(world.factory) as uow:
        owner_role = next(
            role
            for role in await uow.roles.list_for_community(world.community_id)
            if role.is_preset
        )
        membership = await uow.memberships.get_by_user_and_community(
            co_owner, world.community_id
        )
        assert membership is not None
        await uow.memberships.assign_role(membership.id, owner_role.id)
        await uow.commit()
    return co_owner


async def _self_conferral(
    world: _World, uow: SqlAlchemyUnitOfWork, actor: UserId, kind: str
) -> object:
    if kind == "assign-role":
        await AssignRole(uow=uow)(
            community_id=world.community_id,
            user_id=actor,
            role_id=world.starters,
            actor_id=actor,
        )
        return None
    return await CreateGrant(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        actor_id=actor,
        user_id=actor,
        resource_type="server",
        resource_id=world.server_id,
        permissions={_START},
    )


async def _actor_rows(world: _World, actor: UserId) -> int:
    async with world.engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT (SELECT count(*) FROM membership WHERE user_id = :u)"
                        " + (SELECT count(*) FROM resource_grant WHERE user_id = :u)"
                    ),
                    {"u": actor.value},
                )
            ).scalar_one()
        )


@pytest.mark.parametrize("kind", ["assign-role", "create-grant"])
@pytest.mark.parametrize("actor_is_owner", [False, True], ids=["manager", "co-owner"])
@pytest.mark.parametrize("removal_first", [False, True], ids=["conferral", "removal"])
async def test_self_conferral_racing_the_actor_removal_does_not_deadlock(
    engine: AsyncEngine, kind: str, actor_is_owner: bool, removal_first: bool
) -> None:
    # The actor confers to themselves -- a role assignment or a grant, both
    # inserting a row under their own membership -- while they are removed. The
    # conferral locks the actor's authority below that membership, and the
    # removal deletes the membership and cascades to the same rows; whichever
    # holds its first lock is paused there, the other runs into it, and both
    # must then settle without a deadlock: the removal succeeds and the
    # conferral either committed first (and was cascaded away) or is rejected,
    # the membership and the authority under it being gone.
    world = await _world(engine)
    actor = await _add_co_owner(world) if actor_is_owner else world.manager

    def conferral(uow: SqlAlchemyUnitOfWork) -> Coroutine[Any, Any, object]:
        return _self_conferral(world, uow, actor, kind)

    def removal(uow: SqlAlchemyUnitOfWork) -> Coroutine[Any, Any, None]:
        return RemoveMember(uow=uow)(community_id=world.community_id, user_id=actor)

    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    if removal_first:
        pause = _Pause(at="owner-guard")
        removing = asyncio.create_task(
            removal(_PausingUnitOfWork(world.factory, pause))
        )
        await pause.reached.wait()
        conferring = asyncio.create_task(conferral(_PidUnitOfWork(world.factory, pid)))
        await _await_settled(world.engine, conferring, pid)
    else:
        pause = _Pause()
        conferring = asyncio.create_task(
            conferral(_PausingUnitOfWork(world.factory, pause))
        )
        await pause.reached.wait()
        removing = asyncio.create_task(removal(_PidUnitOfWork(world.factory, pid)))
        await _await_settled(world.engine, removing, pid)
    pause.resume.set()
    outcome, removed = await asyncio.gather(
        conferring, removing, return_exceptions=True
    )

    assert not isinstance(removed, BaseException)
    assert not isinstance(outcome, BaseException) or isinstance(
        outcome,
        MembershipNotFoundError
        | GrantTargetNotMemberError
        | PermissionCeilingExceededError,
    )
    assert await _actor_rows(world, actor) == 0


async def test_assignment_added_mid_conferral_does_not_deadlock_a_removal(
    engine: AsyncEngine,
) -> None:
    # The owner's role creation has locked the roles it holds when an
    # assignment of role R to the owner commits. A removal of bystander B (who
    # also holds R) takes the owner's Owner assignment under its last-owner
    # guard, and a deletion of R cascades to both R assignments. Had the
    # conferral then share-locked the new R assignment -- whose role it never
    # locked -- the three would wait on each other in a cycle: conferral on the
    # removal (Owner assignment), removal on the deletion (B's R assignment),
    # deletion on the conferral (the owner's R assignment). R's id sorts before
    # the Owner role's and B's membership before the owner's, so each lock lands
    # in that order.
    world = await _world(engine)
    bystander = UserId(uuid.uuid4())
    await _insert_user(engine, bystander.value, "bystander")
    late_role = replace(
        _role(world.community_id, "Late", {_STOP}), id=RoleId(uuid.UUID(int=1))
    )
    async with SqlAlchemyUnitOfWork(world.factory) as uow:
        await uow.roles.add(late_role)
        await uow.memberships.add(
            Membership(
                id=MembershipId(uuid.UUID(int=2)),
                user_id=bystander,
                community_id=world.community_id,
                created_at=SystemClock().now(),
            )
        )
        await uow.flush()
        await uow.memberships.assign_role(MembershipId(uuid.UUID(int=2)), late_role.id)
        await uow.commit()

    # 1. The conferral locks the owner's roles and pauses.
    conferral_pause = _Pause(at="role-pass")
    conferral_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    conferring = asyncio.create_task(
        CreateRole(
            uow=_PausingUnitOfWork(world.factory, conferral_pause, conferral_pid),
            clock=SystemClock(),
        )(
            community_id=world.community_id,
            actor_id=world.owner,
            name="Launchers",
            permissions={_START},
        )
    )
    await conferral_pause.reached.wait()
    # 2. R is assigned to the owner.
    await AssignRole(uow=SqlAlchemyUnitOfWork(world.factory))(
        community_id=world.community_id,
        user_id=world.owner,
        role_id=late_role.id,
        actor_id=world.owner,
    )
    # 3. B's removal takes B's membership and the Owner assignments, and pauses.
    removal_pause = _Pause(at="owner-guard")
    removing = asyncio.create_task(
        RemoveMember(uow=_PausingUnitOfWork(world.factory, removal_pause))(
            community_id=world.community_id, user_id=bystander
        )
    )
    await removal_pause.reached.wait()
    # 4. The conferral resumes and queues on the owner's Owner assignment.
    conferral_pause.resume.set()
    await _await_settled(world.engine, conferring, conferral_pid)
    # 5. R's deletion runs.
    deletion_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    deleting = asyncio.create_task(
        DeleteRole(uow=_PidUnitOfWork(world.factory, deletion_pid))(
            community_id=world.community_id, role_id=late_role.id
        )
    )
    await _await_settled(world.engine, deleting, deletion_pid)
    # 6. The removal resumes; all three must complete.
    removal_pause.resume.set()
    results = await asyncio.gather(
        conferring, removing, deleting, return_exceptions=True
    )

    for result in results:
        assert not isinstance(result, BaseException)


class _DeletePidServersUnitOfWork(ServersUnitOfWork):
    """Servers unit of work reporting the pid of the transaction that deletes.

    :class:`DeleteServer` enters its unit of work twice (an at-rest check, then
    the deleting transaction); the second entry is the one a lock would block.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pid: asyncio.Future[int],
    ) -> None:
        super().__init__(session_factory)
        self._pid = pid
        self._entries = 0

    async def __aenter__(self) -> _DeletePidServersUnitOfWork:
        await super().__aenter__()
        self._entries += 1
        if self._entries == 2:
            assert self._session is not None
            value = (
                await self._session.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            self._pid.set_result(value)
        return self


async def test_grant_creation_on_the_actor_grant_survives_a_server_deletion(
    engine: AsyncEngine,
) -> None:
    # The creation's ceiling holds the manager's grant on the server; the
    # deletion removes the server and cascades to its grants. Had the creation
    # not held the server first, the deletion would hold the server waiting for
    # the grant while the creation's insert waited for the server: a deadlock.
    world = await _world(engine, start_via_grant=True)

    pause = _Pause()
    creation = asyncio.create_task(
        _create_grant(world, _PausingUnitOfWork(world.factory, pause))
    )
    await pause.reached.wait()
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    deletion = asyncio.create_task(
        DeleteServer(
            uow=_DeletePidServersUnitOfWork(world.factory, pid),
            backup_store=FakeBackupArchiveStore(),
        )(
            community_id=ServersCommunityId(world.community_id.value),
            server_id=ServerId(world.server_id),
        )
    )
    await _await_settled(world.engine, deletion, pid)
    pause.resume.set()
    results = await asyncio.gather(creation, deletion, return_exceptions=True)

    for result in results:
        assert not isinstance(result, BaseException)


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
