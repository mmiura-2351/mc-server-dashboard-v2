"""Integration tests for the community repositories + UnitOfWork on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created and torn down per
test via the real 0004 migration so the adapters run against the documented
shape (DATABASE.md Sections 5-6, 10). Users are inserted with raw SQL because the
community context does not own user creation; the FK to ``user.id`` is exercised
through them.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    MembershipAlreadyExistsError,
    ResourceGrantAlreadyExistsError,
    RoleAlreadyExistsError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
    MembershipId,
    Permission,
    ResourceGrantId,
    RoleId,
    RoleName,
    UserId,
)
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


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


def _community(name: str = "guild") -> Community:
    return Community(
        id=CommunityId.new(),
        name=CommunityName(name),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _role(community_id: CommunityId, name: str = "Owner") -> Role:
    return Role(
        id=RoleId.new(),
        community_id=community_id,
        name=RoleName(name),
        permissions={Permission("server:start"), Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=True,
    )


# --- round-trips -----------------------------------------------------------


async def test_add_community_and_read_back(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    community = _community()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        loaded = await uow.communities.get_by_id(community.id)
        by_name = await uow.communities.get_by_name(CommunityName("guild"))

    assert loaded is not None
    assert loaded.id == community.id
    assert loaded.max_servers is None
    assert loaded.max_members is None
    assert by_name is not None and by_name.id == community.id


async def test_add_role_round_trips_permissions(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    community = _community()
    role = _role(community.id)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.roles.add(role)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        loaded = await uow.roles.get_by_id(role.id)
        listed = await uow.roles.list_for_community(community.id)

    assert loaded is not None
    assert loaded.permissions == role.permissions
    assert loaded.is_preset is True
    assert [r.id for r in listed] == [role.id]


async def test_membership_round_trip_and_lookup(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community = _community()
    membership = Membership(
        id=MembershipId.new(),
        user_id=UserId(user_id),
        community_id=community.id,
        created_at=_NOW,
    )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.memberships.add(membership)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        by_pair = await uow.memberships.get_by_user_and_community(
            UserId(user_id), community.id
        )
    assert by_pair is not None
    assert by_pair.id == membership.id


async def test_resource_grant_round_trip(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community = _community()
    resource_id = uuid.uuid4()
    grant = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(user_id),
        community_id=community.id,
        resource_type="server",
        resource_id=resource_id,
        permissions={Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.resource_grants.add(grant)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        loaded = await uow.resource_grants.get_for_user_resource(
            UserId(user_id), "server", resource_id
        )
    assert loaded is not None
    assert loaded.id == grant.id
    assert loaded.permissions == {Permission("server:stop")}


# --- UnitOfWork semantics --------------------------------------------------


async def test_rollback_when_block_not_committed(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    community = _community()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        # No commit: leaving the block must roll back.

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.communities.get_by_id(community.id) is None


# --- uniqueness constraints ------------------------------------------------


async def test_duplicate_community_name_raises(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(_community("dup"))
        await uow.commit()
    with pytest.raises(CommunityAlreadyExistsError):
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.communities.add(_community("dup"))
            await uow.commit()


async def test_duplicate_role_name_in_community_raises(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    community = _community()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.roles.add(_role(community.id, "Owner"))
        await uow.commit()
    with pytest.raises(RoleAlreadyExistsError):
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.roles.add(_role(community.id, "Owner"))
            await uow.commit()


async def test_same_role_name_in_two_communities_is_allowed(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    a = _community("a")
    b = _community("b")
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(a)
        await uow.communities.add(b)
        await uow.roles.add(_role(a.id, "Owner"))
        await uow.roles.add(_role(b.id, "Owner"))
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert len(await uow.roles.list_for_community(a.id)) == 1
        assert len(await uow.roles.list_for_community(b.id)) == 1


async def test_duplicate_membership_pair_raises(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community = _community()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.memberships.add(
            Membership(
                id=MembershipId.new(),
                user_id=UserId(user_id),
                community_id=community.id,
                created_at=_NOW,
            )
        )
        await uow.commit()
    with pytest.raises(MembershipAlreadyExistsError):
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.memberships.add(
                Membership(
                    id=MembershipId.new(),
                    user_id=UserId(user_id),
                    community_id=community.id,
                    created_at=_NOW,
                )
            )
            await uow.commit()


async def test_duplicate_resource_grant_triple_raises(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community = _community()
    resource_id = uuid.uuid4()

    def _grant() -> ResourceGrant:
        return ResourceGrant(
            id=ResourceGrantId.new(),
            user_id=UserId(user_id),
            community_id=community.id,
            resource_type="server",
            resource_id=resource_id,
            permissions={Permission("server:stop")},
            created_at=_NOW,
            updated_at=_NOW,
        )

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.resource_grants.add(_grant())
        await uow.commit()
    with pytest.raises(ResourceGrantAlreadyExistsError):
        async with SqlAlchemyUnitOfWork(factory) as uow:
            await uow.resource_grants.add(_grant())
            await uow.commit()


# --- cascade behavior (DATABASE.md Section 10) -----------------------------


async def test_deleting_community_cascades_to_all_dependents(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community = _community()
    role = _role(community.id)
    membership = Membership(
        id=MembershipId.new(),
        user_id=UserId(user_id),
        community_id=community.id,
        created_at=_NOW,
    )
    resource_id = uuid.uuid4()
    grant = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(user_id),
        community_id=community.id,
        resource_type="server",
        resource_id=resource_id,
        permissions={Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.roles.add(role)
        await uow.memberships.add(membership)
        await uow.resource_grants.add(grant)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.memberships.assign_role(membership.id, role.id)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.delete(community.id)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.communities.get_by_id(community.id) is None
        assert await uow.memberships.get_by_id(membership.id) is None
        assert await uow.roles.get_by_id(role.id) is None
        assert await uow.resource_grants.get_by_id(grant.id) is None
        assert await uow.memberships.list_role_ids(membership.id) == []
    # The user is global and must survive the community delete (FR-AUTH-5).
    async with engine.connect() as conn:
        count = (
            await conn.execute(
                text('SELECT count(*) FROM "user" WHERE id = :uid'),
                {"uid": user_id},
            )
        ).scalar_one()
    assert count == 1


async def test_deleting_membership_removes_only_membership_role_rows(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community = _community()
    role = _role(community.id)
    membership = Membership(
        id=MembershipId.new(),
        user_id=UserId(user_id),
        community_id=community.id,
        created_at=_NOW,
    )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.roles.add(role)
        await uow.memberships.add(membership)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.memberships.assign_role(membership.id, role.id)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.memberships.delete(membership.id)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.memberships.get_by_id(membership.id) is None
        # The membership_role row is gone (ON DELETE CASCADE).
        assert await uow.memberships.list_role_ids(membership.id) == []
        # The role definition itself survives — it belongs to the community.
        assert await uow.roles.get_by_id(role.id) is not None


# --- use-case grant sweeps (DATABASE.md Section 10) ------------------------


async def test_delete_grants_for_user_in_community_scopes_to_one_community(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community_a = _community("a")
    community_b = _community("b")
    grant_a = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(user_id),
        community_id=community_a.id,
        resource_type="server",
        resource_id=uuid.uuid4(),
        permissions={Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    grant_b = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(user_id),
        community_id=community_b.id,
        resource_type="server",
        resource_id=uuid.uuid4(),
        permissions={Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community_a)
        await uow.communities.add(community_b)
        await uow.resource_grants.add(grant_a)
        await uow.resource_grants.add(grant_b)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.resource_grants.delete_for_user_in_community(
            UserId(user_id), community_a.id
        )
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        # Only community A's grant is swept; community B's is untouched (FR-MEM-3).
        assert await uow.resource_grants.get_by_id(grant_a.id) is None
        assert await uow.resource_grants.get_by_id(grant_b.id) is not None


async def test_delete_grants_for_resource_sweeps_one_resource(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    other_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    await _insert_user(engine, other_id, "bob")
    community = _community()
    server_id = uuid.uuid4()
    other_server_id = uuid.uuid4()
    target = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(user_id),
        community_id=community.id,
        resource_type="server",
        resource_id=server_id,
        permissions={Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    survivor = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(other_id),
        community_id=community.id,
        resource_type="server",
        resource_id=other_server_id,
        permissions={Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.resource_grants.add(target)
        await uow.resource_grants.add(survivor)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.resource_grants.delete_for_resource("server", server_id)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.resource_grants.get_by_id(target.id) is None
        assert await uow.resource_grants.get_by_id(survivor.id) is not None
