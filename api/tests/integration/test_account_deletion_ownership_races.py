"""Account deletion and a conferral of community ownership serialize (#3217).

A deletion refuses an account that owns a community, and its cascade removes the
account's memberships and role assignments. Unless the ownership decision and
the delete are held against a concurrent conferral -- a community provisioned
with the account as its Owner, or the Owner role assigned to it -- the conferral
can commit between the two, and the cascade then takes the new Owner with it.

Each test runs one deletion (self-service or administrator) against one
conferral in one of four interleavings: one side runs to completion before the
other starts, or one side pauses right before its write while the other runs
until it commits or blocks on a lock, and only then resumes. The interleaving is
explicit, not timed. The conferral is a provisioning, or an ownership transfer:
the Owner role assigned to the account, then the previous Owner stepping down,
which on its own would leave the community without an Owner once the account is
deleted. Whichever side comes second fails cleanly -- the deletion is refused as
a community owner, or the conferral finds no such user -- and no interleaving
leaves a community without an Owner. Two conferrals on one account, by contrast,
do not wait for each other.

Adding the account as a member, or creating a grant for it, is held the same way
(#3856), though it confers nothing: each inserts a row whose foreign key
references the account. Unheld, a deletion committing after the write's check
fails that insert's foreign key, and a grant creation, which holds the account's
membership before its insert, deadlocks with the deletion's cascade. Run through
the same four interleavings, the losing write finds no such user, and no
interleaving ends in an error the routes do not map.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, replace
from typing import Any, Literal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mc_server_dashboard_api.community.adapters.clock import SystemClock
from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyMembershipRepository,
)
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.adapters.user_directory import (
    IdentityUserDirectory,
)
from mc_server_dashboard_api.community.application.manage_grant import CreateGrant
from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
    AssignRole,
    UnassignRole,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.errors import (
    GrantTargetNotMemberError,
    LastOwnerRemovalError,
    MembershipNotFoundError,
    MemberUserNotFoundError,
    OwnerUserNotFoundError,
)
from mc_server_dashboard_api.community.domain.permissions import OWNER_ROLE_NAME
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    MembershipId,
    Permission,
    RoleId,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    UserId as CommunityUserId,
)
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.repositories import (
    SqlAlchemyUserRepository,
)
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as IdentityUnitOfWork,
)
from mc_server_dashboard_api.identity.application.admin_delete_user import (
    AdminDeleteUser,
)
from mc_server_dashboard_api.identity.application.delete_account import (
    DeleteAccount,
)
from mc_server_dashboard_api.identity.domain.errors import CommunityOwnedError
from mc_server_dashboard_api.identity.domain.value_objects import UserId
from tests.identity.fakes import FakeClock, StubHasher, make_user
from tests.integration.races import SETTLE_TIMEOUT, await_settled, race_database

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_PASSWORD = "Wm7!qz#Lp2vT"
_COMMUNITY = "guild"
_OTHER_COMMUNITY = "outpost"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


class _Pause:
    """Holds a use case right before its write until released."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        if self.reached.is_set():
            return
        self.reached.set()
        await self.resume.wait()


class _Backend:
    """The backend pid of a step's current transaction, once it has one."""

    pid: int | None = None


async def _record_pid(session: AsyncSession, backend: _Backend) -> None:
    backend.pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()


class _PausingUserRepository(SqlAlchemyUserRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def delete(self, user_id: UserId) -> None:
        await self._pause.hold()
        await super().delete(user_id)


class _IdentityUnitOfWork(IdentityUnitOfWork):
    """Records its backend pid; with a pause, holds right before the user DELETE."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        backend: _Backend,
        pause: _Pause | None,
    ) -> None:
        super().__init__(session_factory)
        self._backend = backend
        self._pause = pause

    async def __aenter__(self) -> _IdentityUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        await _record_pid(self._session, self._backend)
        if self._pause is not None:
            self.users = _PausingUserRepository(self._session, self._pause)
        return self


class _PausingMembershipRepository(SqlAlchemyMembershipRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def assign_role(self, membership_id: MembershipId, role_id: RoleId) -> None:
        await self._pause.hold()
        await super().assign_role(membership_id, role_id)


class _CommunityUnitOfWork(CommunityUnitOfWork):
    """Records its backend pid; with a pause, holds right before its write.

    That is the Owner assignment, or else the commit, which flushes the one row
    a member addition or a grant creation staged.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        backend: _Backend,
        pause: _Pause | None,
    ) -> None:
        super().__init__(session_factory)
        self._backend = backend
        self._pause = pause

    async def __aenter__(self) -> _CommunityUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        await _record_pid(self._session, self._backend)
        if self._pause is not None:
            self.memberships = _PausingMembershipRepository(self._session, self._pause)
        return self

    async def commit(self) -> None:
        if self._pause is not None:
            await self._pause.hold()
        await super().commit()


@dataclass(frozen=True)
class _World:
    factory: async_sessionmaker[AsyncSession]
    # The account the deletion deletes and the community write names.
    target: uuid.UUID
    # The community, its Owner role and its Owner (a transfer's previous Owner).
    community: CommunityId | None = None
    owner_role: RoleId | None = None
    owner: uuid.UUID | None = None
    # A server of the community, for a grant.
    server: uuid.UUID | None = None


async def _provision_community(
    uow: CommunityUnitOfWork, owner: uuid.UUID, name: str = _COMMUNITY
) -> Community:
    return await ProvisionCommunity(uow=uow, clock=SystemClock())(
        name=name, owner_user_id=CommunityUserId(owner)
    )


_Deletion = Callable[[_World, IdentityUnitOfWork], Coroutine[Any, Any, None]]
_Write = Callable[[_World, CommunityUnitOfWork], Coroutine[Any, Any, None]]


async def _admin_delete(world: _World, uow: IdentityUnitOfWork) -> None:
    # The acting admin is irrelevant to the guards; only "not the target" is.
    await AdminDeleteUser(uow=uow, clock=FakeClock(_NOW))(
        actor_id=UserId.new(), target_id=UserId(world.target)
    )


async def _delete_account(world: _World, uow: IdentityUnitOfWork) -> None:
    await DeleteAccount(uow=uow, hasher=StubHasher(), clock=FakeClock(_NOW))(
        user_id=UserId(world.target), password=_PASSWORD
    )


async def _provision(world: _World, uow: CommunityUnitOfWork) -> None:
    await _provision_community(uow, world.target)


async def _provision_other(world: _World, uow: CommunityUnitOfWork) -> None:
    await _provision_community(uow, world.target, _OTHER_COMMUNITY)


async def _assign_owner(world: _World, uow: CommunityUnitOfWork) -> None:
    assert world.community is not None and world.owner_role is not None
    assert world.owner is not None
    await AssignRole(uow=uow)(
        community_id=world.community,
        user_id=CommunityUserId(world.target),
        role_id=world.owner_role,
        actor_id=CommunityUserId(world.owner),
    )


async def _step_down(world: _World, uow: CommunityUnitOfWork) -> None:
    assert world.community is not None and world.owner_role is not None
    assert world.owner is not None
    await UnassignRole(uow=uow)(
        community_id=world.community,
        user_id=CommunityUserId(world.owner),
        role_id=world.owner_role,
    )


async def _add_member(world: _World, uow: CommunityUnitOfWork) -> None:
    assert world.community is not None
    await AddMember(
        uow=uow,
        users=IdentityUserDirectory(IdentityUnitOfWork(world.factory)),
        clock=SystemClock(),
    )(community_id=world.community, user_id=CommunityUserId(world.target))


async def _create_grant(world: _World, uow: CommunityUnitOfWork) -> None:
    assert world.community is not None and world.owner is not None
    assert world.server is not None
    await CreateGrant(uow=uow, clock=SystemClock())(
        community_id=world.community,
        actor_id=CommunityUserId(world.owner),
        user_id=CommunityUserId(world.target),
        resource_type="server",
        resource_id=world.server,
        permissions={Permission("server:start")},
    )


async def _seed_user(
    factory: async_sessionmaker[AsyncSession], username: str
) -> UserId:
    user = make_user(username=username, email=f"{username}@example.com")
    async with IdentityUnitOfWork(factory) as uow:
        await uow.users.add(user)
        await uow.commit()
    return user.id


async def _provisioning_world(engine: AsyncEngine) -> _World:
    factory = create_session_factory(engine)
    target = await _seed_user(factory, "target")
    return _World(factory, target.value)


async def _transfer_world(engine: AsyncEngine) -> _World:
    """The target is a plain member of a community whose only Owner is another."""

    factory = create_session_factory(engine)
    target = await _seed_user(factory, "target")
    previous = await _seed_user(factory, "previous")
    community = await _provision_community(CommunityUnitOfWork(factory), previous.value)
    await AddMember(
        uow=CommunityUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )(community_id=community.id, user_id=CommunityUserId(target.value))
    async with CommunityUnitOfWork(factory) as uow:
        (owner_role,) = await uow.roles.list_for_community(community.id)
    return _World(factory, target.value, community.id, owner_role.id, previous.value)


async def _member_world(engine: AsyncEngine) -> _World:
    """The target is in no community; another account owns the community."""

    factory = create_session_factory(engine)
    target = await _seed_user(factory, "target")
    owner = await _seed_user(factory, "owner")
    community = await _provision_community(CommunityUnitOfWork(factory), owner.value)
    return _World(factory, target.value, community.id, owner=owner.value)


async def _grant_world(engine: AsyncEngine) -> _World:
    """As above, with the target a plain member and a server in the community."""

    world = await _member_world(engine)
    await _add_member(world, CommunityUnitOfWork(world.factory))
    assert world.community is not None
    server = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server "
                "(id, community_id, name, mc_edition, mc_version, server_type, "
                "config, slug, desired_state, observed_state, "
                "created_at, updated_at) VALUES "
                "(:id, :cid, 'survival', 'java', '1.21', 'vanilla', "
                "'{}'::jsonb, 'survival-00', 'stopped', 'stopped', now(), now())"
            ),
            {"id": server, "cid": world.community.value},
        )
    return replace(world, server=server)


_Interleaving = Literal[
    "deletion-first", "write-first", "deletion-holds", "write-holds"
]
_INTERLEAVINGS: list[_Interleaving] = [
    "deletion-first",
    "write-first",
    "deletion-holds",
    "write-holds",
]

# A step of the race: the deletion, or one community write. It runs on a unit
# of work that reports its backend pid and, given a pause, holds there.
_Step = Callable[[_Backend, _Pause | None], Coroutine[Any, Any, None]]


def _deleting(world: _World, deletion: _Deletion) -> _Step:
    async def step(backend: _Backend, pause: _Pause | None) -> None:
        await deletion(world, _IdentityUnitOfWork(world.factory, backend, pause))

    return step


def _writing(world: _World, write: _Write) -> _Step:
    async def step(backend: _Backend, pause: _Pause | None) -> None:
        await write(world, _CommunityUnitOfWork(world.factory, backend, pause))

    return step


async def _race(
    engine: AsyncEngine, steps: list[_Step], interleaving: _Interleaving
) -> list[BaseException | None]:
    """Run the deletion (``steps[0]``) and the community writes in ``interleaving``.

    Returns each step's outcome (``None`` or the exception it raised), in the
    order of ``steps``.
    """

    if interleaving in ("deletion-first", "write-first"):
        order = list(range(len(steps)))
        if interleaving == "write-first":
            order = [*order[1:], 0]
        sequential: dict[int, BaseException | None] = {}
        for index in order:
            try:
                await steps[index](_Backend(), None)
            except Exception as exc:
                sequential[index] = exc
            else:
                sequential[index] = None
        return [sequential[index] for index in range(len(steps))]

    # Pause the deletion, or the first community write, before it writes.
    paused = 0 if interleaving == "deletion-holds" else 1
    pause = _Pause()
    tasks = {paused: asyncio.create_task(steps[paused](_Backend(), pause))}
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)
    for index, step in enumerate(steps):
        if index == paused:
            continue
        backend = _Backend()
        tasks[index] = asyncio.create_task(step(backend, None))
        await await_settled(engine, tasks[index], lambda: backend.pid)
    pause.resume.set()
    return list(
        await asyncio.wait_for(
            asyncio.gather(
                *(tasks[index] for index in range(len(steps))),
                return_exceptions=True,
            ),
            SETTLE_TIMEOUT,
        )
    )


async def _owners(engine: AsyncEngine) -> dict[str, set[uuid.UUID]]:
    """Map each community's name to the users holding its preset Owner role."""

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.name, m.user_id FROM community c"
                    " LEFT JOIN role r ON r.community_id = c.id"
                    " AND r.is_preset AND r.name = :owner"
                    " LEFT JOIN membership_role mr ON mr.role_id = r.id"
                    " LEFT JOIN membership m ON m.id = mr.membership_id"
                ),
                {"owner": OWNER_ROLE_NAME},
            )
        ).all()
    owners: dict[str, set[uuid.UUID]] = {}
    for name, user_id in rows:
        holders = owners.setdefault(name, set())
        if user_id is not None:
            holders.add(user_id)
    return owners


def _kinds(outcomes: list[BaseException | None]) -> list[type[BaseException] | None]:
    return [None if outcome is None else type(outcome) for outcome in outcomes]


_DELETIONS = {"admin-delete": _admin_delete, "delete-account": _delete_account}


@pytest.mark.parametrize("interleaving", _INTERLEAVINGS)
@pytest.mark.parametrize("deletion", list(_DELETIONS), ids=list(_DELETIONS))
async def test_deletion_racing_provisioning_never_orphans_the_community(
    engine: AsyncEngine, deletion: str, interleaving: _Interleaving
) -> None:
    world = await _provisioning_world(engine)

    outcomes = await _race(
        engine,
        [_deleting(world, _DELETIONS[deletion]), _writing(world, _provision)],
        interleaving,
    )

    owners = await _owners(engine)
    assert all(owners.values()), f"a community has no Owner: {owners}"
    if interleaving in ("deletion-first", "deletion-holds"):
        # The deletion committed first: the provisioning finds no such user.
        assert _kinds(outcomes) == [None, OwnerUserNotFoundError]
        assert owners == {}
    else:
        # The provisioning committed first: the account now owns a community.
        assert _kinds(outcomes) == [CommunityOwnedError, None]
        assert owners == {_COMMUNITY: {world.target}}


# What a transfer's steps -- the deletion, the Owner assignment, the previous
# Owner stepping down -- end in, by interleaving.
_TRANSFER_OUTCOMES: dict[_Interleaving, list[type[BaseException] | None]] = {
    # The deletion committed first: the assignment finds no such member, so the
    # previous Owner is still the only one and may not step down.
    "deletion-first": [None, MembershipNotFoundError, LastOwnerRemovalError],
    "deletion-holds": [None, MembershipNotFoundError, LastOwnerRemovalError],
    # The assignment committed first: the account owns the community.
    "write-first": [CommunityOwnedError, None, None],
    # As above; the step-down started while the assignment was not yet visible
    # and waited for its locks, so it still counts one Owner and is refused.
    "write-holds": [CommunityOwnedError, None, LastOwnerRemovalError],
}
_TRANSFER_OWNERS: dict[_Interleaving, str] = {
    "deletion-first": "previous",
    "deletion-holds": "previous",
    "write-first": "target",
    "write-holds": "both",
}


@pytest.mark.parametrize("interleaving", _INTERLEAVINGS)
@pytest.mark.parametrize("deletion", list(_DELETIONS), ids=list(_DELETIONS))
async def test_deletion_racing_an_ownership_transfer_never_orphans_the_community(
    engine: AsyncEngine, deletion: str, interleaving: _Interleaving
) -> None:
    world = await _transfer_world(engine)
    assert world.owner is not None

    outcomes = await _race(
        engine,
        [
            _deleting(world, _DELETIONS[deletion]),
            _writing(world, _assign_owner),
            _writing(world, _step_down),
        ],
        interleaving,
    )

    owners = await _owners(engine)
    assert all(owners.values()), f"a community has no Owner: {owners}"
    assert _kinds(outcomes) == _TRANSFER_OUTCOMES[interleaving]
    expected = {
        "previous": {world.owner},
        "target": {world.target},
        "both": {world.owner, world.target},
    }[_TRANSFER_OWNERS[interleaving]]
    assert owners == {_COMMUNITY: expected}


# A write that inserts a row referencing the account, the world it runs in, and
# what it fails with once the account is gone.
_INSERTS = {
    "add-member": (_member_world, _add_member, MemberUserNotFoundError),
    "create-grant": (_grant_world, _create_grant, GrantTargetNotMemberError),
}


@pytest.mark.parametrize("interleaving", _INTERLEAVINGS)
@pytest.mark.parametrize("insert", list(_INSERTS), ids=list(_INSERTS))
@pytest.mark.parametrize("deletion", list(_DELETIONS), ids=list(_DELETIONS))
async def test_deletion_racing_an_insert_referencing_the_account_fails_cleanly(
    engine: AsyncEngine, deletion: str, insert: str, interleaving: _Interleaving
) -> None:
    build, write, refusal = _INSERTS[insert]
    world = await build(engine)

    outcomes = await _race(
        engine,
        [_deleting(world, _DELETIONS[deletion]), _writing(world, write)],
        interleaving,
    )

    if interleaving in ("deletion-first", "deletion-holds"):
        # The deletion committed first: the write finds no such user.
        assert _kinds(outcomes) == [None, refusal]
    else:
        # The write committed first, and the deletion's cascade removed its row.
        assert _kinds(outcomes) == [None, None]


async def test_conferrals_on_one_account_do_not_wait_for_each_other(
    engine: AsyncEngine,
) -> None:
    # Conferrals share the account write lock: a second one on the same account
    # commits while the first, paused before its Owner write, still holds it.
    world = await _provisioning_world(engine)
    pause = _Pause()
    first = asyncio.create_task(_writing(world, _provision)(_Backend(), pause))
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)
    backend = _Backend()
    second = asyncio.create_task(_writing(world, _provision_other)(backend, None))
    await await_settled(engine, second, lambda: backend.pid)
    waited = not second.done()
    pause.resume.set()
    await asyncio.wait_for(asyncio.gather(first, second), SETTLE_TIMEOUT)

    assert not waited, "the second conferral waited for the first"
    assert await _owners(engine) == {
        _COMMUNITY: {world.target},
        _OTHER_COMMUNITY: {world.target},
    }
