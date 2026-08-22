"""Integration tests for community provisioning + delete cascade on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service); skipped
otherwise (TESTING.md Section 5). Exercises the ProvisionCommunity use case end to
end with the real SqlAlchemy UnitOfWork and the IdentityUserDirectory against the
0004 schema, verifying the Owner role / membership / assignment are seeded
atomically (FR-COMM-4), that an unknown owner leaves nothing behind, and that
deleting the community cascades to every dependent (DATABASE.md Section 10).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

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
    RenameCommunity,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.errors import (
    CommunityNotFoundError,
    OwnerUserNotFoundError,
)
from mc_server_dashboard_api.community.domain.permissions import (
    COMMUNITY_PERMISSIONS,
    OWNER_ROLE_NAME,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
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


def _provision(engine: AsyncEngine) -> ProvisionCommunity:
    factory = create_session_factory(engine)
    return ProvisionCommunity(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )


async def test_provision_seeds_owner_role_membership_and_assignment(
    engine: AsyncEngine,
) -> None:
    owner_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")

    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))

    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        loaded = await uow.communities.get_by_name(CommunityName("guild"))
        assert loaded is not None and loaded.id == community.id

        roles = await uow.roles.list_for_community(community.id)
        assert len(roles) == 1
        owner_role = roles[0]
        assert owner_role.name == RoleName(OWNER_ROLE_NAME)
        assert owner_role.is_preset is True
        assert owner_role.permissions == set(COMMUNITY_PERMISSIONS)

        membership = await uow.memberships.get_by_user_and_community(
            UserId(owner_id), community.id
        )
        assert membership is not None
        assert await uow.memberships.list_role_ids(membership.id) == [owner_role.id]


async def test_provision_unknown_owner_persists_nothing(engine: AsyncEngine) -> None:
    with pytest.raises(OwnerUserNotFoundError):
        await _provision(engine)(name="guild", owner_user_id=UserId(uuid.uuid4()))

    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.communities.get_by_name(CommunityName("guild")) is None


async def test_delete_community_cascades_to_dependents(engine: AsyncEngine) -> None:
    owner_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))

    factory = create_session_factory(engine)
    await DeleteCommunity(uow=SqlAlchemyUnitOfWork(factory))(community_id=community.id)

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.communities.get_by_id(community.id) is None
        assert (
            await uow.memberships.get_by_user_and_community(
                UserId(owner_id), community.id
            )
            is None
        )
        assert await uow.roles.list_for_community(community.id) == []

    # The owner is a global user and must survive the community delete (FR-AUTH-5).
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text('SELECT count(*) FROM "user" WHERE id = :uid'),
                {"uid": owner_id},
            )
        ).scalar_one()
    assert count == 1


# --- a write on a community a racer deleted (issue #2613) ---------------------


class _DeleteOnLoadCommunityRepository(SqlAlchemyCommunityRepository):
    """A community repository that deletes the row right after handing it back.

    Reproduces the production interleave deterministically, with no sleeps: the
    use case's ``get_by_id`` read succeeds, another request's ``DeleteCommunity``
    commits on its own connection, and only then does ``update`` run its UPDATE
    against a row that is gone.
    """

    def __init__(self, session: AsyncSession, engine: AsyncEngine) -> None:
        super().__init__(session)
        self._engine = engine

    async def get_by_id(self, community_id: CommunityId) -> Community | None:
        community = await super().get_by_id(community_id)
        async with self._engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM community WHERE id = :id"), {"id": community_id.value}
            )
        return community


class _RacingCommunityUnitOfWork(SqlAlchemyUnitOfWork):
    """A UnitOfWork wired with :class:`_DeleteOnLoadCommunityRepository`."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine
    ) -> None:
        super().__init__(session_factory)
        self._engine = engine

    async def __aenter__(self) -> _RacingCommunityUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.communities = _DeleteOnLoadCommunityRepository(self._session, self._engine)
        return self


async def test_rename_community_reports_a_concurrent_delete_as_not_found(
    engine: AsyncEngine,
) -> None:
    # ``RenameCommunity`` losing to ``DeleteCommunity`` used to return 200
    # carrying the new name for a row that no longer existed: the UPDATE matched
    # zero rows and nothing looked at the count. The rowcount is now the existence
    # assertion, so the racer gets the same 404 the use case's own pre-read would
    # have raised had the delete landed a moment earlier.
    owner_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))
    factory = create_session_factory(engine)

    with pytest.raises(CommunityNotFoundError):
        await RenameCommunity(
            uow=_RacingCommunityUnitOfWork(factory, engine), clock=SystemClock()
        )(community_id=community.id, name="renamed")
