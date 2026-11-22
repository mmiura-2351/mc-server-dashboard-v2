"""A write whose community is deleted mid-write answers not-found (#2967).

The route's gate admitted the caller as a member a moment earlier, then the
community's deletion commits before the write does. Whichever statement of the
write meets the deletion first, the write raises the community context's
:class:`CommunityNotFoundError`, which the routes answer with the gate's own
non-member 404:

- a member add or a role creation that confers nothing takes no lock of this
  context before its INSERT, so the INSERT fails its foreign key onto
  ``community`` -- at once when the deletion has committed, or after waiting
  for one in flight;
- a role creation that confers permissions first holds the actor's membership
  for the permission ceiling (#3241), and finds it gone: the deletion cascaded
  it away. The ceiling is then empty, but because the community is gone, not
  because the actor lacks the permissions;
- a role assignment and a grant creation hold the recipient's membership before
  the actor's ceiling, so they already find no recipient, and answer the
  not-found they answer for any non-member (pinned here, unchanged by #2967).

A grant's own foreign key onto ``community`` is not reachable this way: once the
grant holds its memberships, the deletion's cascade waits for them, so the
deletion cannot commit before the grant's INSERT. It is pinned at the adapter.

Each race pauses the deletion after its DELETE, lets the write run on another
connection until it blocks on a lock, then resumes the deletion. The opposite
order -- the write holding its locks first -- is not tested, because it reaches
no not-found: a member add, a role creation conferring nothing and a role
assignment commit first and are cascaded away, while a role or grant creation
holding the ceiling's locks waits for the community row the deletion holds and
deadlocks with it, which PostgreSQL resolves by aborting one side
(``permission_ceiling.py``).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mc_server_dashboard_api.community.adapters.clock import SystemClock
from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyCommunityRepository,
)
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.community.adapters.user_directory import (
    IdentityUserDirectory,
)
from mc_server_dashboard_api.community.application.manage_community import (
    DeleteCommunity,
)
from mc_server_dashboard_api.community.application.manage_grant import CreateGrant
from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
    AssignRole,
    RemoveMember,
)
from mc_server_dashboard_api.community.application.manage_role import CreateRole
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.errors import (
    CommunityNotFoundError,
    GrantTargetNotMemberError,
    MembershipNotFoundError,
    PermissionCeilingExceededError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
    ResourceGrantId,
    RoleId,
    UserId,
)
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as IdentityUnitOfWork,
)
from tests.integration.races import await_settled, race_database

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_START = Permission("server:start")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


class _Pause:
    """Holds the deletion after its DELETE, before its COMMIT, until released."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.resume.wait()


class _PausingCommunityRepository(SqlAlchemyCommunityRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def delete(self, community_id: CommunityId) -> None:
        await super().delete(community_id)
        await self._pause.hold()


class _UnitOfWork(SqlAlchemyUnitOfWork):
    """Reports its backend pid, or pauses a deletion after its DELETE."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        pid: asyncio.Future[int] | None = None,
        pause: _Pause | None = None,
    ) -> None:
        super().__init__(session_factory)
        self._pid = pid
        self._pause = pause

    async def __aenter__(self) -> _UnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        if self._pid is not None:
            result = await self._session.execute(text("SELECT pg_backend_pid()"))
            self._pid.set_result(result.scalar_one())
        if self._pause is not None:
            self.communities = _PausingCommunityRepository(self._session, self._pause)
        return self


@dataclass(frozen=True)
class _World:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    community_id: CommunityId
    owner: UserId
    # A plain member, holding no role.
    member: UserId
    # A user who is not a member.
    outsider: UserId
    owner_role: RoleId


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


def _add_member(factory: async_sessionmaker[AsyncSession]) -> AddMember:
    return AddMember(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )


async def _world(engine: AsyncEngine) -> _World:
    owner, member, outsider = (UserId(uuid.uuid4()) for _ in range(3))
    await _insert_user(engine, owner.value, "owner")
    await _insert_user(engine, member.value, "member")
    await _insert_user(engine, outsider.value, "outsider")
    factory = create_session_factory(engine)
    community = await ProvisionCommunity(
        uow=SqlAlchemyUnitOfWork(factory), clock=SystemClock()
    )(name="guild", owner_user_id=owner)
    await _add_member(factory)(community_id=community.id, user_id=member)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        owner_role = next(
            role
            for role in await uow.roles.list_for_community(community.id)
            if role.is_preset
        )
    return _World(engine, factory, community.id, owner, member, outsider, owner_role.id)


async def _delete_community(world: _World) -> None:
    await DeleteCommunity(uow=SqlAlchemyUnitOfWork(world.factory))(
        community_id=world.community_id
    )


# A write by the owner on behalf of the community, run on the given unit of work.
_Write = Callable[[_World, SqlAlchemyUnitOfWork], Coroutine[Any, Any, object]]


async def _add_outsider(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    return await AddMember(
        uow=uow,
        users=IdentityUserDirectory(IdentityUnitOfWork(world.factory)),
        clock=SystemClock(),
    )(community_id=world.community_id, user_id=world.outsider)


async def _create_empty_role(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    # Confers nothing, so the ceiling is not consulted and nothing is locked
    # before the INSERT.
    return await CreateRole(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        actor_id=world.owner,
        name="Empty",
        permissions=set(),
    )


async def _create_starter_role(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    return await CreateRole(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        actor_id=world.owner,
        name="Starters",
        permissions={_START},
    )


async def _assign_owner_role(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    await AssignRole(uow=uow)(
        community_id=world.community_id,
        user_id=world.member,
        role_id=world.owner_role,
        actor_id=world.owner,
    )
    return None


async def _create_grant(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    # No server: a community holding one cannot be deleted (#3218).
    return await CreateGrant(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        actor_id=world.owner,
        user_id=world.member,
        resource_type="server",
        resource_id=uuid.uuid4(),
        permissions={_START},
    )


_NOT_FOUND_WRITES = [
    pytest.param(_add_outsider, id="add-member"),
    pytest.param(_create_empty_role, id="create-role-conferring-nothing"),
    pytest.param(_create_starter_role, id="create-role-conferring-permissions"),
]


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return int(
            (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        )


async def _run_after_the_deletion_committed(world: _World, write: _Write) -> object:
    await _delete_community(world)
    try:
        return await write(world, SqlAlchemyUnitOfWork(world.factory))
    except Exception as exc:  # the outcome under test
        return exc


async def _run_racing_an_uncommitted_deletion(world: _World, write: _Write) -> object:
    """Run ``write`` against a deletion that has deleted but not committed.

    The write must block on the deletion: what it reacts to is not committed
    yet. The deletion then commits, and the write's outcome is returned.
    """

    pause = _Pause()
    deletion = asyncio.create_task(
        DeleteCommunity(uow=_UnitOfWork(world.factory, pause=pause))(
            community_id=world.community_id
        )
    )
    await pause.reached.wait()

    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    writing = asyncio.create_task(write(world, _UnitOfWork(world.factory, pid=pid)))
    await await_settled(
        world.engine, writing, lambda: pid.result() if pid.done() else None
    )
    assert not writing.done()
    pause.resume.set()
    deleted, outcome = await asyncio.gather(deletion, writing, return_exceptions=True)
    assert deleted is None
    return outcome


@pytest.mark.parametrize("write", _NOT_FOUND_WRITES)
async def test_write_after_the_community_was_deleted_reports_not_found(
    engine: AsyncEngine, write: _Write
) -> None:
    world = await _world(engine)

    outcome = await _run_after_the_deletion_committed(world, write)

    assert isinstance(outcome, CommunityNotFoundError)
    assert await _count(engine, "membership") == 0
    assert await _count(engine, "role") == 0


@pytest.mark.parametrize("write", _NOT_FOUND_WRITES)
async def test_write_racing_an_uncommitted_deletion_reports_not_found(
    engine: AsyncEngine, write: _Write
) -> None:
    world = await _world(engine)

    outcome = await _run_racing_an_uncommitted_deletion(world, write)

    assert isinstance(outcome, CommunityNotFoundError)
    assert await _count(engine, "membership") == 0
    assert await _count(engine, "role") == 0


@pytest.mark.parametrize(
    ("write", "not_found"),
    [
        pytest.param(_assign_owner_role, MembershipNotFoundError, id="assign-role"),
        pytest.param(_create_grant, GrantTargetNotMemberError, id="create-grant"),
    ],
)
@pytest.mark.parametrize("in_flight", [False, True], ids=["committed", "in-flight"])
async def test_write_under_a_recipient_reports_the_recipient_not_found(
    engine: AsyncEngine, write: _Write, not_found: type[Exception], in_flight: bool
) -> None:
    # The recipient's membership is held before the actor's ceiling, and the
    # deletion took it with the community: the write answers the not-found it
    # answers for any non-member recipient, which the routes map to the same
    # 404 as CommunityNotFoundError. The ceiling's community check is never
    # reached.
    world = await _world(engine)

    if in_flight:
        outcome = await _run_racing_an_uncommitted_deletion(world, write)
    else:
        outcome = await _run_after_the_deletion_committed(world, write)

    assert isinstance(outcome, not_found)


async def test_ceiling_of_a_removed_member_of_a_live_community_stays_forbidden(
    engine: AsyncEngine,
) -> None:
    # The actor's membership is gone, but the community is not: the ceiling is
    # empty because the actor holds nothing here, which is what the 403 says.
    world = await _world(engine)
    await RemoveMember(uow=SqlAlchemyUnitOfWork(world.factory))(
        community_id=world.community_id, user_id=world.member
    )

    with pytest.raises(PermissionCeilingExceededError):
        await CreateRole(uow=SqlAlchemyUnitOfWork(world.factory), clock=SystemClock())(
            community_id=world.community_id,
            actor_id=world.member,
            name="Starters",
            permissions={_START},
        )


async def test_grant_committed_after_its_community_was_deleted_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # Not reachable through CreateGrant (see the module docstring), so staged
    # directly. The INSERT fails both its community key and its membership key
    # (the membership went with the community); PostgreSQL checks them in its
    # constraint triggers' name order, which embeds their OIDs, so either may
    # be the one reported, and both answer the same 404. The server lives in
    # another community, which a community holding one could not be deleted
    # without.
    world = await _world(engine)
    async with SqlAlchemyUnitOfWork(world.factory) as uow:
        membership = await uow.memberships.get_by_user_and_community(
            world.member, world.community_id
        )
    assert membership is not None
    other = await ProvisionCommunity(
        uow=SqlAlchemyUnitOfWork(world.factory), clock=SystemClock()
    )(name="other", owner_user_id=world.owner)
    server_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server "
                "(id, community_id, name, mc_edition, mc_version, server_type, "
                "config, slug, desired_state, observed_state, "
                "created_at, updated_at) VALUES "
                "(:id, :cid, 'survival', 'java', '1.21', 'vanilla', "
                "'{}'::jsonb, 'srv-other-00', 'stopped', 'stopped', now(), now())"
            ),
            {"id": server_id, "cid": other.id.value},
        )
    await _delete_community(world)

    now = SystemClock().now()
    grant = ResourceGrant(
        id=ResourceGrantId.new(),
        membership_id=membership.id,
        user_id=world.member,
        community_id=world.community_id,
        resource_type="server",
        resource_id=server_id,
        permissions={_START},
        created_at=now,
        updated_at=now,
    )
    with pytest.raises((CommunityNotFoundError, GrantTargetNotMemberError)):
        async with SqlAlchemyUnitOfWork(world.factory) as uow:
            await uow.resource_grants.add(grant)
            await uow.commit()
    assert await _count(engine, "resource_grant") == 0
