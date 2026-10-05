"""A grant must not outlive a concurrently removed membership or server (#3216).

Each test pauses one :class:`CreateGrant` after all its checks have passed, right
before it stages the grant, lets the membership removal or server deletion run on
another connection, then resumes the paused creation. ``resource_grant`` carries
foreign keys to the membership instance it belongs to (by id, so a re-added
member's new membership is a different parent) and to the server it targets,
both ``ON DELETE CASCADE``, so whichever order the transactions land in, no grant
survives its parent: either the deletion commits first and the creation's INSERT
fails its foreign key (surfacing as the typed not-found error the route maps to
404), or the creation commits first and the deletion's cascade removes the grant.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5), mirroring ``test_community_stale_role_writes.py``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from mc_server_dashboard_api.community.adapters.clock import SystemClock
from mc_server_dashboard_api.community.adapters.permission_checker import (
    RoleGrantPermissionChecker,
)
from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyResourceGrantRepository,
)
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.community.adapters.user_directory import (
    IdentityUserDirectory,
)
from mc_server_dashboard_api.community.application.manage_grant import CreateGrant
from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
    RemoveMember,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.errors import (
    GrantResourceNotFoundError,
    GrantTargetNotMemberError,
    MembershipAlreadyExistsError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
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


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


class _Pause:
    """Holds a creation right before it stages the grant, until released."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.resume.wait()


class _PausingResourceGrantRepository(SqlAlchemyResourceGrantRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def add(self, grant: ResourceGrant) -> None:
        await self._pause.hold()
        await super().add(grant)


class _PausingUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], pause: _Pause
    ) -> None:
        super().__init__(session_factory)
        self._pause = pause

    async def __aenter__(self) -> _PausingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.resource_grants = _PausingResourceGrantRepository(
            self._session, self._pause
        )
        return self


async def _await_settled(
    engine: AsyncEngine, competitor: asyncio.Task[Any], pid: asyncio.Future[int]
) -> None:
    await await_settled(
        engine, competitor, lambda: pid.result() if pid.done() else None
    )
    if competitor.done():
        competitor.result()


@dataclass(frozen=True)
class _World:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    community_id: CommunityId
    owner: UserId
    member: UserId
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


def _add_member(factory: async_sessionmaker[AsyncSession]) -> AddMember:
    return AddMember(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )


async def _world(engine: AsyncEngine) -> _World:
    owner = UserId(uuid.uuid4())
    member = UserId(uuid.uuid4())
    await _insert_user(engine, owner.value, "owner")
    await _insert_user(engine, member.value, "member")
    factory = create_session_factory(engine)
    community = await ProvisionCommunity(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )(name="guild", owner_user_id=owner)
    await _add_member(factory)(community_id=community.id, user_id=member)
    server_id = uuid.uuid4()
    await _insert_server(engine, server_id, community.id)
    return _World(engine, factory, community.id, owner, member, server_id)


def _paused_creation(world: _World, pause: _Pause) -> asyncio.Task[ResourceGrant]:
    return asyncio.create_task(
        CreateGrant(uow=_PausingUnitOfWork(world.factory, pause), clock=SystemClock())(
            community_id=world.community_id,
            actor_id=world.owner,
            user_id=world.member,
            resource_type="server",
            resource_id=world.server_id,
            permissions={_START},
        )
    )


async def _race(
    world: _World,
    pause: _Pause,
    creation: asyncio.Task[ResourceGrant],
    competitor: asyncio.Task[None],
    pid: asyncio.Future[int],
) -> BaseException | ResourceGrant:
    await _await_settled(world.engine, competitor, pid)
    pause.resume.set()
    outcome, _ = await asyncio.gather(creation, competitor, return_exceptions=True)
    competitor.result()
    assert isinstance(outcome, BaseException | ResourceGrant)
    return outcome


async def _count(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


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


async def test_grant_racing_member_removal_leaves_no_grant(engine: AsyncEngine) -> None:
    world = await _world(engine)

    pause = _Pause()
    creation = _paused_creation(world, pause)
    await pause.reached.wait()

    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    removal = asyncio.create_task(
        RemoveMember(uow=_PidUnitOfWork(world.factory, pid))(
            community_id=world.community_id, user_id=world.member
        )
    )
    outcome = await _race(world, pause, creation, removal, pid)

    # The losing creation reports a typed not-found, never success-with-a-ghost
    # nor a raw database error; a creation that won was cascaded away.
    assert not isinstance(outcome, BaseException) or isinstance(
        outcome, GrantTargetNotMemberError
    )
    assert (
        await _count(
            engine,
            "SELECT count(*) FROM resource_grant WHERE user_id = :u",
            u=world.member.value,
        )
        == 0
    )


async def test_rejoining_after_a_raced_removal_restores_no_permission(
    engine: AsyncEngine,
) -> None:
    world = await _world(engine)

    pause = _Pause()
    creation = _paused_creation(world, pause)
    await pause.reached.wait()
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    removal = asyncio.create_task(
        RemoveMember(uow=_PidUnitOfWork(world.factory, pid))(
            community_id=world.community_id, user_id=world.member
        )
    )
    await _race(world, pause, creation, removal, pid)

    # Re-adding the user without any role must not resurrect the grant.
    await _add_member(world.factory)(
        community_id=world.community_id, user_id=world.member
    )
    can_start = await RoleGrantPermissionChecker(
        SqlAlchemyUnitOfWork(world.factory)
    ).can(
        user=AuthUser(user_id=world.member),
        operation=_START,
        resource=ResourceRef(
            community_id=world.community_id,
            resource_type="server",
            resource_id=world.server_id,
        ),
    )
    assert can_start is False


async def test_grant_cannot_attach_to_a_membership_re_added_mid_creation(
    engine: AsyncEngine,
) -> None:
    # The creation validated the original membership; the user is removed and
    # re-added (without roles) before it resumes. The grant must not attach to
    # the replacement membership, which nobody granted anything. Since the
    # creation holds the membership it validated (#3241), the removal queues
    # behind it and the re-add is refused as a duplicate meanwhile; either
    # way, the user ends up without the grant.
    world = await _world(engine)

    pause = _Pause()
    creation = _paused_creation(world, pause)
    await pause.reached.wait()

    removal_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    removal = asyncio.create_task(
        RemoveMember(uow=_PidUnitOfWork(world.factory, removal_pid))(
            community_id=world.community_id, user_id=world.member
        )
    )
    await _await_settled(world.engine, removal, removal_pid)
    (re_added,) = await asyncio.gather(
        _add_member(world.factory)(
            community_id=world.community_id, user_id=world.member
        ),
        return_exceptions=True,
    )
    assert not isinstance(re_added, BaseException) or isinstance(
        re_added, MembershipAlreadyExistsError
    )
    pause.resume.set()
    outcome, removed = await asyncio.gather(creation, removal, return_exceptions=True)
    assert not isinstance(removed, BaseException)

    assert not isinstance(outcome, BaseException) or isinstance(
        outcome, GrantTargetNotMemberError
    )
    can_start = await RoleGrantPermissionChecker(
        SqlAlchemyUnitOfWork(world.factory)
    ).can(
        user=AuthUser(user_id=world.member),
        operation=_START,
        resource=ResourceRef(
            community_id=world.community_id,
            resource_type="server",
            resource_id=world.server_id,
        ),
    )
    assert can_start is False


async def test_grant_racing_server_deletion_leaves_no_grant(
    engine: AsyncEngine,
) -> None:
    world = await _world(engine)

    pause = _Pause()
    creation = _paused_creation(world, pause)
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
    outcome = await _race(world, pause, creation, deletion, pid)

    assert not isinstance(outcome, BaseException) or isinstance(
        outcome, GrantResourceNotFoundError
    )
    assert (
        await _count(
            engine, "SELECT count(*) FROM server WHERE id = :s", s=world.server_id
        )
        == 0
    )
    assert (
        await _count(
            engine,
            "SELECT count(*) FROM resource_grant WHERE resource_id = :s",
            s=world.server_id,
        )
        == 0
    )


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
