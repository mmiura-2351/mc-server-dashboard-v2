"""Integration tests for role and grant management on PostgreSQL (issue #71).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service); skipped
otherwise (TESTING.md Section 5). Exercises the role/grant use cases end to end
with the real SqlAlchemy UnitOfWork against the 0004 schema:

- a custom role's duplicate name surfaces as the translated 409 domain error;
- deleting a non-preset role cascades its ``membership_role`` assignments via the
  DB FK (``ondelete=CASCADE``, DATABASE.md Section 10);
- a grant on the M1 ``server`` type persists, and a duplicate
  ``(user, resource_type, resource_id)`` surfaces as the translated 409;
- cross-community role/grant ids are unusable via another community's use cases
  (reported as not-found — FR-AUTHZ-4).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.clock import SystemClock
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
)
from mc_server_dashboard_api.community.application.manage_role import (
    CreateRole,
    DeleteRole,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.errors import (
    ResourceGrantAlreadyExistsError,
    ResourceGrantNotFoundError,
    RoleAlreadyExistsError,
    RoleNotFoundError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
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


async def _insert_server(
    engine: AsyncEngine, server_id: uuid.UUID, community_id: CommunityId
) -> None:
    """Insert a minimal ``server`` row directly so the grant's resource exists
    (the resource-existence checker rejects fabricated ids; issue #361)."""

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server "
                "(id, community_id, name, mc_edition, mc_version, server_type, "
                "config, slug, desired_state, observed_state, "
                "created_at, updated_at) VALUES "
                "(:id, :cid, :name, 'java', '1.21', 'vanilla', "
                "'{}'::jsonb, :slug, 'stopped', 'stopped', now(), now())"
            ),
            {
                "id": server_id,
                "cid": community_id.value,
                "name": f"srv-{server_id}",
                "slug": f"srv-{str(server_id)[:8]}-00",
            },
        )


def _provision(engine: AsyncEngine) -> ProvisionCommunity:
    factory = create_session_factory(engine)
    return ProvisionCommunity(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )


def _add_member(engine: AsyncEngine) -> AddMember:
    factory = create_session_factory(engine)
    return AddMember(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )


async def test_create_role_duplicate_name_is_translated_to_conflict(
    engine: AsyncEngine,
) -> None:
    owner_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))
    factory = create_session_factory(engine)
    create = CreateRole(uow=SqlAlchemyUnitOfWork(factory), clock=SystemClock())

    await create(
        community_id=community.id,
        name="Editor",
        permissions={Permission("server:read")},
    )
    with pytest.raises(RoleAlreadyExistsError):
        await create(
            community_id=community.id,
            name="Editor",
            permissions={Permission("server:start")},
        )


async def test_delete_role_cascades_membership_assignments(
    engine: AsyncEngine,
) -> None:
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, member_id, "bob")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))
    await _add_member(engine)(community_id=community.id, user_id=UserId(member_id))

    factory = create_session_factory(engine)
    role = await CreateRole(uow=SqlAlchemyUnitOfWork(factory), clock=SystemClock())(
        community_id=community.id,
        name="Editor",
        permissions={Permission("server:read")},
    )
    await AssignRole(uow=SqlAlchemyUnitOfWork(factory))(
        community_id=community.id, user_id=UserId(member_id), role_id=role.id
    )

    async with engine.connect() as conn:
        before = (
            await conn.execute(
                text("SELECT count(*) FROM membership_role WHERE role_id = :rid"),
                {"rid": role.id.value},
            )
        ).scalar_one()
    assert before == 1

    await DeleteRole(uow=SqlAlchemyUnitOfWork(factory))(
        community_id=community.id, role_id=role.id
    )

    async with engine.connect() as conn:
        after = (
            await conn.execute(
                text("SELECT count(*) FROM membership_role WHERE role_id = :rid"),
                {"rid": role.id.value},
            )
        ).scalar_one()
    assert after == 0


async def test_create_grant_persists_and_duplicate_is_translated(
    engine: AsyncEngine,
) -> None:
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, member_id, "bob")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))
    await _add_member(engine)(community_id=community.id, user_id=UserId(member_id))

    factory = create_session_factory(engine)
    create = CreateGrant(uow=SqlAlchemyUnitOfWork(factory), clock=SystemClock())
    resource_id = uuid.uuid4()
    await _insert_server(engine, resource_id, community.id)
    grant = await create(
        community_id=community.id,
        user_id=UserId(member_id),
        resource_type="server",
        resource_id=resource_id,
        permissions={Permission("server:start")},
    )

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert await uow.resource_grants.get_by_id(grant.id) is not None

    with pytest.raises(ResourceGrantAlreadyExistsError):
        await create(
            community_id=community.id,
            user_id=UserId(member_id),
            resource_type="server",
            resource_id=resource_id,
            permissions={Permission("server:stop")},
        )


async def test_cross_community_role_and_grant_ids_are_not_found(
    engine: AsyncEngine,
) -> None:
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    member_a = uuid.uuid4()
    await _insert_user(engine, owner_a, "alice")
    await _insert_user(engine, owner_b, "carol")
    await _insert_user(engine, member_a, "bob")
    community_a = await _provision(engine)(name="a", owner_user_id=UserId(owner_a))
    community_b = await _provision(engine)(name="b", owner_user_id=UserId(owner_b))
    await _add_member(engine)(community_id=community_a.id, user_id=UserId(member_a))

    factory = create_session_factory(engine)
    role = await CreateRole(uow=SqlAlchemyUnitOfWork(factory), clock=SystemClock())(
        community_id=community_a.id,
        name="Editor",
        permissions={Permission("server:read")},
    )
    resource_id = uuid.uuid4()
    await _insert_server(engine, resource_id, community_a.id)
    grant = await CreateGrant(uow=SqlAlchemyUnitOfWork(factory), clock=SystemClock())(
        community_id=community_a.id,
        user_id=UserId(member_a),
        resource_type="server",
        resource_id=resource_id,
        permissions={Permission("server:start")},
    )

    # Community B cannot delete A's role or revoke A's grant: reported as not-found.
    with pytest.raises(RoleNotFoundError):
        await DeleteRole(uow=SqlAlchemyUnitOfWork(factory))(
            community_id=community_b.id, role_id=role.id
        )
    with pytest.raises(ResourceGrantNotFoundError):
        await RevokeGrant(uow=SqlAlchemyUnitOfWork(factory))(
            community_id=community_b.id, grant_id=grant.id
        )
