"""Integration tests for the PermissionChecker evaluator on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Exercises the role+grant evaluator and
the Layer-1 membership visibility against the real 0004 schema, so the set math
runs over rows the adapters actually persist (DATABASE.md Sections 5-6). Users
are inserted with raw SQL because the community context does not own user
creation.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.permission_checker import (
    RepositoryMembershipVisibility,
    RoleGrantPermissionChecker,
)
from mc_server_dashboard_api.community.adapters.unit_of_work import SqlAlchemyUnitOfWork
from mc_server_dashboard_api.community.domain.entities import (
    Community,
    Membership,
    ResourceGrant,
    Role,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    CommunityName,
    MembershipId,
    Permission,
    ResourceGrantId,
    ResourceRef,
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


def _role(community_id: CommunityId, permissions: set[Permission], name: str) -> Role:
    return Role(
        id=RoleId.new(),
        community_id=community_id,
        name=RoleName(name),
        permissions=permissions,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _membership(user_id: uuid.UUID, community_id: CommunityId) -> Membership:
    return Membership(
        id=MembershipId.new(),
        user_id=UserId(user_id),
        community_id=community_id,
        created_at=_NOW,
    )


async def test_role_permission_grants_operation(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community = _community()
    role = _role(community.id, {Permission("server:start")}, "Op")
    membership = _membership(user_id, community.id)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.roles.add(role)
        await uow.memberships.add(membership)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.memberships.assign_role(membership.id, role.id)
        await uow.commit()

    checker = RoleGrantPermissionChecker(SqlAlchemyUnitOfWork(factory))
    allowed = await checker.can(
        user=AuthUser(user_id=UserId(user_id)),
        operation=Permission("server:start"),
        resource=ResourceRef(community_id=community.id),
    )
    assert allowed is True


async def test_resource_grant_scoped_to_exact_resource(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community = _community()
    membership = _membership(user_id, community.id)
    server_x = uuid.uuid4()
    server_y = uuid.uuid4()
    grant = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(user_id),
        community_id=community.id,
        resource_type="server",
        resource_id=server_x,
        permissions={Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.memberships.add(membership)
        await uow.resource_grants.add(grant)
        await uow.commit()

    checker = RoleGrantPermissionChecker(SqlAlchemyUnitOfWork(factory))
    on_x = await checker.can(
        user=AuthUser(user_id=UserId(user_id)),
        operation=Permission("server:stop"),
        resource=ResourceRef(
            community_id=community.id, resource_type="server", resource_id=server_x
        ),
    )
    on_y = await checker.can(
        user=AuthUser(user_id=UserId(user_id)),
        operation=Permission("server:stop"),
        resource=ResourceRef(
            community_id=community.id, resource_type="server", resource_id=server_y
        ),
    )
    assert on_x is True
    assert on_y is False


async def test_resource_grant_does_not_apply_in_a_different_community(
    engine: AsyncEngine,
) -> None:
    # Defense-in-depth (FR-AUTHZ-4): a grant on (server, X) in community A must
    # not satisfy a check scoped to community B for the same resource id.
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community_a = _community("a")
    community_b = _community("b")
    membership_b = _membership(user_id, community_b.id)
    server_id = uuid.uuid4()
    grant = ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(user_id),
        community_id=community_a.id,
        resource_type="server",
        resource_id=server_id,
        permissions={Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community_a)
        await uow.communities.add(community_b)
        await uow.memberships.add(membership_b)
        await uow.resource_grants.add(grant)
        await uow.commit()

    checker = RoleGrantPermissionChecker(SqlAlchemyUnitOfWork(factory))
    in_b = await checker.can(
        user=AuthUser(user_id=UserId(user_id)),
        operation=Permission("server:stop"),
        resource=ResourceRef(
            community_id=community_b.id, resource_type="server", resource_id=server_id
        ),
    )
    assert in_b is False


async def test_cross_community_isolation(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community_a = _community("a")
    community_b = _community("b")
    role_a = _role(community_a.id, {Permission("server:start")}, "Op")
    membership_a = _membership(user_id, community_a.id)
    membership_b = _membership(user_id, community_b.id)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community_a)
        await uow.communities.add(community_b)
        await uow.roles.add(role_a)
        await uow.memberships.add(membership_a)
        await uow.memberships.add(membership_b)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.memberships.assign_role(membership_a.id, role_a.id)
        await uow.commit()

    checker = RoleGrantPermissionChecker(SqlAlchemyUnitOfWork(factory))
    in_b = await checker.can(
        user=AuthUser(user_id=UserId(user_id)),
        operation=Permission("server:start"),
        resource=ResourceRef(community_id=community_b.id),
    )
    assert in_b is False


async def test_platform_admin_axis_independent_of_membership(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    checker = RoleGrantPermissionChecker(SqlAlchemyUnitOfWork(factory))
    allowed = await checker.can(
        user=AuthUser(user_id=UserId(uuid.uuid4()), is_platform_admin=True),
        operation=Permission("worker:manage"),
        resource=ResourceRef(community_id=CommunityId.new()),
    )
    assert allowed is True


async def test_membership_visibility(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    user_id = uuid.uuid4()
    await _insert_user(engine, user_id, "alice")
    community = _community()
    other = _community("other")
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.communities.add(other)
        await uow.memberships.add(_membership(user_id, community.id))
        await uow.commit()

    visibility = RepositoryMembershipVisibility(SqlAlchemyUnitOfWork(factory))
    assert (
        await visibility.is_member(user_id=UserId(user_id), community_id=community.id)
        is True
    )
    assert (
        await visibility.is_member(user_id=UserId(user_id), community_id=other.id)
        is False
    )
