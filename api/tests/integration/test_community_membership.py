"""Integration tests for membership management on PostgreSQL (FR-MEM-1/2/3).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service); skipped
otherwise (TESTING.md Section 5). Exercises the membership use cases end to end
with the real SqlAlchemy UnitOfWork against the 0004 schema:

- add-member duplicate surfaces as the translated 409 domain error;
- remove-member sweeps the user's roles AND grants in this community atomically,
  while leaving the *same user's* grants in another community intact (FR-MEM-2/3);
- the last-Owner guard refuses to orphan a community;
- role assignment validates the role belongs to this community (cross-community
  assignment must fail — FR-AUTHZ-4).
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
from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
    AssignRole,
    ListMembers,
    RemoveMember,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.errors import (
    LastOwnerRemovalError,
    MembershipAlreadyExistsError,
    MemberUserNotFoundError,
    RoleNotFoundError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
    ResourceGrantId,
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


def _add_member(engine: AsyncEngine) -> AddMember:
    factory = create_session_factory(engine)
    return AddMember(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )


async def _grant(
    engine: AsyncEngine,
    user_id: UserId,
    community_id: CommunityId,
    resource_id: uuid.UUID,
) -> None:
    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        await uow.resource_grants.add(
            ResourceGrant(
                id=ResourceGrantId.new(),
                user_id=user_id,
                community_id=community_id,
                resource_type="server",
                resource_id=resource_id,
                permissions={Permission("server:start")},
                created_at=SystemClock().now(),
                updated_at=SystemClock().now(),
            )
        )
        await uow.commit()


async def test_add_member_duplicate_is_translated_to_conflict(
    engine: AsyncEngine,
) -> None:
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, member_id, "bob")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))

    await _add_member(engine)(community_id=community.id, user_id=UserId(member_id))
    with pytest.raises(MembershipAlreadyExistsError):
        await _add_member(engine)(community_id=community.id, user_id=UserId(member_id))


async def test_add_member_by_username_resolves_case_insensitively(
    engine: AsyncEngine,
) -> None:
    # A non-admin owner adds the member by exact username; resolution is
    # case-insensitive, mirroring the identity uniqueness key (issue #355).
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, member_id, "Bob")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))

    membership = await _add_member(engine)(community_id=community.id, username="bob")

    assert membership.user_id == UserId(member_id)


async def test_add_member_by_unknown_username_is_rejected(
    engine: AsyncEngine,
) -> None:
    owner_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))

    with pytest.raises(MemberUserNotFoundError):
        await _add_member(engine)(community_id=community.id, username="ghost")


async def test_remove_member_sweeps_roles_and_grants_atomically(
    engine: AsyncEngine,
) -> None:
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, member_id, "bob")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))
    await _add_member(engine)(community_id=community.id, user_id=UserId(member_id))
    await _grant(engine, UserId(member_id), community.id, uuid.uuid4())

    factory = create_session_factory(engine)
    await RemoveMember(uow=SqlAlchemyUnitOfWork(factory))(
        community_id=community.id, user_id=UserId(member_id)
    )

    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert (
            await uow.memberships.get_by_user_and_community(
                UserId(member_id), community.id
            )
            is None
        )
    # No membership_role rows remain for any deleted membership (cascade) and no
    # grants remain for the removed user in this community.
    async with engine.connect() as conn:
        grant_count = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM resource_grant "
                    "WHERE user_id = :uid AND community_id = :cid"
                ),
                {"uid": member_id, "cid": community.id.value},
            )
        ).scalar_one()
    assert grant_count == 0


async def test_remove_member_keeps_grants_in_other_communities(
    engine: AsyncEngine,
) -> None:
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_a, "alice")
    await _insert_user(engine, owner_b, "carol")
    await _insert_user(engine, member_id, "bob")
    community_a = await _provision(engine)(name="a", owner_user_id=UserId(owner_a))
    community_b = await _provision(engine)(name="b", owner_user_id=UserId(owner_b))
    await _add_member(engine)(community_id=community_a.id, user_id=UserId(member_id))
    await _add_member(engine)(community_id=community_b.id, user_id=UserId(member_id))
    await _grant(engine, UserId(member_id), community_a.id, uuid.uuid4())
    await _grant(engine, UserId(member_id), community_b.id, uuid.uuid4())

    factory = create_session_factory(engine)
    await RemoveMember(uow=SqlAlchemyUnitOfWork(factory))(
        community_id=community_b.id, user_id=UserId(member_id)
    )

    async with engine.connect() as conn:
        a_grants = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM resource_grant "
                    "WHERE user_id = :uid AND community_id = :cid"
                ),
                {"uid": member_id, "cid": community_a.id.value},
            )
        ).scalar_one()
        b_grants = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM resource_grant "
                    "WHERE user_id = :uid AND community_id = :cid"
                ),
                {"uid": member_id, "cid": community_b.id.value},
            )
        ).scalar_one()
    # A survives, B is swept (FR-MEM-2/3 scoping).
    assert a_grants == 1
    assert b_grants == 0


async def test_remove_last_owner_is_rejected(engine: AsyncEngine) -> None:
    owner_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))

    factory = create_session_factory(engine)
    with pytest.raises(LastOwnerRemovalError):
        await RemoveMember(uow=SqlAlchemyUnitOfWork(factory))(
            community_id=community.id, user_id=UserId(owner_id)
        )
    # Still a member after the rejected removal.
    async with SqlAlchemyUnitOfWork(factory) as uow:
        assert (
            await uow.memberships.get_by_user_and_community(
                UserId(owner_id), community.id
            )
            is not None
        )


async def test_assign_role_from_another_community_fails(engine: AsyncEngine) -> None:
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_a, "alice")
    await _insert_user(engine, owner_b, "carol")
    await _insert_user(engine, member_id, "bob")
    community_a = await _provision(engine)(name="a", owner_user_id=UserId(owner_a))
    community_b = await _provision(engine)(name="b", owner_user_id=UserId(owner_b))
    await _add_member(engine)(community_id=community_a.id, user_id=UserId(member_id))

    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        (b_role,) = await uow.roles.list_for_community(community_b.id)

    # Assigning community B's Owner role to a member of community A must fail.
    with pytest.raises(RoleNotFoundError):
        await AssignRole(uow=SqlAlchemyUnitOfWork(factory))(
            community_id=community_a.id,
            user_id=UserId(member_id),
            role_id=b_role.id,
        )


async def test_assign_role_in_community_succeeds_and_lists(
    engine: AsyncEngine,
) -> None:
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, member_id, "bob")
    community = await _provision(engine)(name="guild", owner_user_id=UserId(owner_id))
    await _add_member(engine)(community_id=community.id, user_id=UserId(member_id))

    factory = create_session_factory(engine)
    async with SqlAlchemyUnitOfWork(factory) as uow:
        (owner_role,) = await uow.roles.list_for_community(community.id)

    await AssignRole(uow=SqlAlchemyUnitOfWork(factory))(
        community_id=community.id, user_id=UserId(member_id), role_id=owner_role.id
    )

    members = await ListMembers(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
    )(community_id=community.id)
    by_user = {view.user_id: view for view in members}
    assert owner_role.name.value in by_user[UserId(member_id)].role_names
    # Usernames are resolved through the directory seam (issue #78).
    assert by_user[UserId(owner_id)].username == "alice"
    assert by_user[UserId(member_id)].username == "bob"
