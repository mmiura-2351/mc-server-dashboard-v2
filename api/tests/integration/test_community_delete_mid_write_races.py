"""A community deletion and a write of that community never deadlock (#4021).

The route's gate admitted the caller as a member a moment earlier, and the
community's deletion then races the write. A deletion locks the community row
before anything it cascades to, and every write of this context that locks a
row of that cascade and then another lock holds the community row first
(``FOR KEY SHARE``; the lock order in ``permission_ceiling.py``). Whichever
locks the row first, the other waits for it:

- **The deletion first.** The write waits for the community row and, once the
  deletion has committed, raises :class:`CommunityNotFoundError`, which the
  routes answer with the gate's own non-member 404 (#2967). A member add and a
  role creation that confers nothing lock nothing of this context before their
  INSERT, which meets the deletion at its foreign key onto ``community``
  instead, with the same answer. Each race pauses the deletion after its DELETE,
  lets the write run on another connection until it blocks on a lock, then
  resumes the deletion.
- **The write first.** The deletion waits for the write to commit, then cascades
  its row away -- or, when the community holds a server, as it does for any
  grant, is refused (#3218). Each race pauses the write while it holds its
  locks, lets the deletion run until it blocks, then resumes the write. Where
  the two deadlocked before #4021, the race runs a second time with the write
  resumed only after the deletion has waited past ``deadlock_timeout``: the
  deletion's deadlock check has then run and found nothing, and the write, not
  the deletion, was the victim.

Some of those deadlocks need the deletion to cascade to roles or grants before
memberships. A deletion runs a row's cascades in the order of their trigger
names, which embed OIDs, so the schema does not fix that order; those races
recreate foreign keys to change it (:func:`_cascade_last`).

A grant's own foreign key onto ``community`` is not reachable through
``CreateGrant``: it holds the community row before its INSERT. It is pinned at
the adapter.

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
    UnassignRole,
)
from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    UpdateRole,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.errors import (
    CommunityHasServersError,
    CommunityNotFoundError,
    GrantTargetNotMemberError,
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
from tests.integration.races import SETTLE_TIMEOUT, await_settled, race_database

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_START = Permission("server:start")
_STOP = Permission("server:stop")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


class _Pause:
    """Holds a transaction at a chosen point, before its COMMIT, until released."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.resume.wait()


def _pause_after(owner: object, name: str, pause: _Pause) -> None:
    """Make ``owner.name`` hold at ``pause`` each time it has returned."""

    method = getattr(owner, name)

    async def paused(*args: Any, **kwargs: Any) -> Any:
        result = await method(*args, **kwargs)
        await pause.hold()
        return result

    setattr(owner, name, paused)


class _UnitOfWork(SqlAlchemyUnitOfWork):
    """Reports its backend pid, or pauses after one of its repositories' calls.

    ``pause_after`` names the call as ``"<repository>.<method>"``; by default
    the deletion's DELETE.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        pid: asyncio.Future[int] | None = None,
        pause: _Pause | None = None,
        pause_after: str = "communities.delete",
    ) -> None:
        super().__init__(session_factory)
        self._pid = pid
        self._pause = pause
        self._pause_after = pause_after

    async def __aenter__(self) -> _UnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        if self._pid is not None:
            result = await self._session.execute(text("SELECT pg_backend_pid()"))
            self._pid.set_result(result.scalar_one())
        if self._pause is not None:
            repository, method = self._pause_after.split(".")
            _pause_after(getattr(self, repository), method, self._pause)
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
    # A custom role conferring server:start, held by the member.
    builders: RoleId
    # The id of the community's server, which exists only once _add_server ran.
    server_id: uuid.UUID


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
    builders = await CreateRole(uow=SqlAlchemyUnitOfWork(factory), clock=SystemClock())(
        community_id=community.id,
        actor_id=owner,
        name="Builders",
        permissions={_START},
    )
    await AssignRole(uow=SqlAlchemyUnitOfWork(factory))(
        community_id=community.id,
        user_id=member,
        role_id=builders.id,
        actor_id=owner,
    )
    return _World(
        engine,
        factory,
        community.id,
        owner,
        member,
        outsider,
        owner_role.id,
        builders.id,
        uuid.uuid4(),
    )


async def _add_server(world: _World) -> None:
    async with world.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server "
                "(id, community_id, name, mc_edition, mc_version, server_type, "
                "config, slug, desired_state, observed_state, "
                "created_at, updated_at) VALUES "
                "(:id, :cid, 'survival', 'java', '1.21', 'vanilla', "
                "'{}'::jsonb, 'srv-guild-00', 'stopped', 'stopped', now(), now())"
            ),
            {"id": world.server_id, "cid": world.community_id.value},
        )


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
    # On the community's server, which a deletion that is to succeed leaves
    # absent: a community holding one cannot be deleted (#3218).
    return await CreateGrant(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        actor_id=world.owner,
        user_id=world.member,
        resource_type="server",
        resource_id=world.server_id,
        permissions={_START},
    )


async def _update_builders(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    return await UpdateRole(uow=uow, clock=SystemClock())(
        community_id=world.community_id,
        role_id=world.builders,
        actor_id=world.owner,
        permissions={_START, _STOP},
    )


async def _remove_member(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    await RemoveMember(uow=uow)(community_id=world.community_id, user_id=world.member)
    return None


async def _unassign_owner_role(world: _World, uow: SqlAlchemyUnitOfWork) -> object:
    # The member does not hold it, but unassigning the Owner role runs the
    # last-owner guard, which locks the Owner assignments.
    await UnassignRole(uow=uow)(
        community_id=world.community_id,
        user_id=world.member,
        role_id=world.owner_role,
    )
    return None


_NOT_FOUND_WRITES = [
    pytest.param(_add_outsider, id="add-member"),
    pytest.param(_create_empty_role, id="create-role-conferring-nothing"),
    pytest.param(_create_starter_role, id="create-role-conferring-permissions"),
    pytest.param(_update_builders, id="update-role"),
    pytest.param(_assign_owner_role, id="assign-role"),
    pytest.param(_create_grant, id="create-grant"),
    pytest.param(_remove_member, id="remove-member"),
    pytest.param(_unassign_owner_role, id="unassign-role"),
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


async def _deadlock_timeout(engine: AsyncEngine) -> float:
    async with engine.connect() as conn:
        setting = await conn.execute(
            text("SELECT setting::int FROM pg_settings WHERE name = 'deadlock_timeout'")
        )
        return int(setting.scalar_one()) / 1000


async def _run_a_deletion_racing_an_uncommitted_write(
    world: _World, write: _Write, pause_after: str, *, stalled: bool
) -> tuple[object, object]:
    """Run a deletion against ``write`` paused after ``pause_after``.

    The deletion must block on the write, which holds locks the deletion needs.
    The write is then resumed -- at once, or, ``stalled``, only once the
    deletion has waited past ``deadlock_timeout``, so that PostgreSQL's deadlock
    check, which a backend runs once it has waited that long, has run in the
    deletion before the write takes its next lock. Returns the write's and the
    deletion's outcomes.
    """

    pause = _Pause()
    writing = asyncio.create_task(
        write(world, _UnitOfWork(world.factory, pause=pause, pause_after=pause_after))
    )
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)

    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    deletion = asyncio.create_task(
        DeleteCommunity(uow=_UnitOfWork(world.factory, pid=pid))(
            community_id=world.community_id
        )
    )
    await await_settled(
        world.engine, deletion, lambda: pid.result() if pid.done() else None
    )
    assert not deletion.done()
    if stalled:
        await asyncio.sleep(await _deadlock_timeout(world.engine) + 0.5)
    pause.resume.set()
    written, deleted = await asyncio.gather(writing, deletion, return_exceptions=True)
    return written, deleted


async def _cascade_last(engine: AsyncEngine, *tables: str) -> None:
    """Recreate these tables' foreign keys onto ``community``, in this order.

    A recreated key's triggers get the newest OIDs, so a deletion now runs these
    cascades after the others, in the order given -- unless an OID gained a digit
    in between, since trigger names sort as strings. The fixed lock order does
    not depend on it either way.
    """

    async with engine.begin() as conn:
        for table in tables:
            key = f"fk_{table}_community_id_community"
            action = "RESTRICT" if table == "server" else "CASCADE"
            await conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {key}"))
            await conn.execute(
                text(
                    f"ALTER TABLE {table} ADD CONSTRAINT {key} FOREIGN KEY "
                    f"(community_id) REFERENCES community (id) ON DELETE {action}"
                )
            )


def _write_first(
    write: _Write,
    pause_after: str,
    name: str,
    *,
    cascade_last: tuple[str, ...] = (),
    stall: bool = False,
) -> list[Any]:
    """The race of ``write`` paused after ``pause_after``, and stalled if ``stall``."""

    return [
        pytest.param(
            write,
            pause_after,
            cascade_last,
            stalled,
            id=f"{name}-stalled" if stalled else name,
        )
        for stalled in ((False, True) if stall else (False,))
    ]


@pytest.mark.parametrize(
    ("write", "pause_after", "cascade_last", "stalled"),
    [
        *_write_first(
            _create_starter_role,
            "memberships.lock_role_ids",
            "create-role-conferring-permissions",
            stall=True,
        ),
        *_write_first(_update_builders, "roles.lock_by_id", "update-role"),
        *_write_first(
            _update_builders,
            "memberships.hold_for_users",
            "update-role-roles-cascading-first",
            cascade_last=("membership",),
            stall=True,
        ),
        *_write_first(_assign_owner_role, "memberships.list_role_ids", "assign-role"),
        *_write_first(
            _assign_owner_role,
            "memberships.hold_for_users",
            "assign-role-roles-cascading-first",
            cascade_last=("membership",),
            stall=True,
        ),
        *_write_first(
            _remove_member, "memberships.lock_owner_role_holders", "remove-member"
        ),
        *_write_first(
            _unassign_owner_role,
            "memberships.lock_owner_role_holders",
            "unassign-role",
        ),
    ],
)
async def test_deletion_waiting_for_a_write_cascades_it(
    engine: AsyncEngine,
    write: _Write,
    pause_after: str,
    cascade_last: tuple[str, ...],
    stalled: bool,
) -> None:
    world = await _world(engine)
    await _cascade_last(engine, *cascade_last)

    written, deleted = await _run_a_deletion_racing_an_uncommitted_write(
        world, write, pause_after, stalled=stalled
    )

    assert not isinstance(written, BaseException), written
    assert deleted is None, deleted
    for table in ("membership", "role", "membership_role"):
        assert await _count(engine, table) == 0


@pytest.mark.parametrize(
    ("write", "pause_after", "cascade_last", "stalled"),
    [
        *_write_first(
            _create_grant,
            "resource_grants.lock_for_user_resource",
            "create-grant",
            stall=True,
        ),
        # Holding only its memberships yet: the community row comes before them.
        *_write_first(
            _create_grant, "memberships.hold_for_users", "create-grant-first-lock"
        ),
    ],
)
async def test_deletion_waiting_for_a_grant_is_refused(
    engine: AsyncEngine,
    write: _Write,
    pause_after: str,
    cascade_last: tuple[str, ...],
    stalled: bool,
) -> None:
    # A grant names a server of the community, and a community holding a server
    # is not deleted (#3218): the deletion waits for the grant, then is refused.
    world = await _world(engine)
    await _add_server(world)

    written, deleted = await _run_a_deletion_racing_an_uncommitted_write(
        world, write, pause_after, stalled=stalled
    )

    assert isinstance(written, ResourceGrant), written
    assert isinstance(deleted, CommunityHasServersError), deleted
    assert await _count(engine, "resource_grant") == 1


@pytest.mark.parametrize("stalled", [False, True], ids=["prompt", "stalled"])
async def test_deletion_waiting_for_the_removal_of_a_granted_member_is_refused(
    engine: AsyncEngine, stalled: bool
) -> None:
    # The removal cascades to the member's grant. A deletion that cascades to
    # grants before memberships, and checks for servers only after them, took
    # that grant before it waited for the removal's membership lock.
    world = await _world(engine)
    await _add_server(world)
    await _create_grant(world, SqlAlchemyUnitOfWork(world.factory))
    await _cascade_last(engine, "membership", "server")

    written, deleted = await _run_a_deletion_racing_an_uncommitted_write(
        world, _remove_member, "memberships.lock_by_user_and_community", stalled=stalled
    )

    assert written is None, written
    assert isinstance(deleted, CommunityHasServersError), deleted
    assert await _count(engine, "membership") == 1
    assert await _count(engine, "resource_grant") == 0


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
