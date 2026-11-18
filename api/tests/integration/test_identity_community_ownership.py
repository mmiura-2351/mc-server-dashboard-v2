"""Integration tests for the CommunityBackedOwnership adapter on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Exercises the cross-context adapter
identity's self-delete guard uses (``owns_any_community``) against a real
database, seeding the community and its owner through the community context's own
ProvisionCommunity / AddMember flows (mirroring test_community_membership.py) so
the multi-hop membership -> role -> role-id query is verified end to end: true
for the owner, false for a plain member and for a non-member.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.clock import SystemClock
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.adapters.user_directory import (
    IdentityUserDirectory,
)
from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
    AssignRole,
)
from mc_server_dashboard_api.community.application.manage_role import CreateRole
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.value_objects import Permission
from mc_server_dashboard_api.community.domain.value_objects import (
    UserId as CommunityUserId,
)
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.identity.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as IdentityUnitOfWork,
)
from mc_server_dashboard_api.identity.domain.value_objects import UserId
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
        uow=CommunityUnitOfWork(factory),
        clock=SystemClock(),
    )


def _add_member(engine: AsyncEngine) -> AddMember:
    factory = create_session_factory(engine)
    return AddMember(
        uow=CommunityUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )


async def _owns_any_community(engine: AsyncEngine, user_id: uuid.UUID) -> bool:
    # Bound on the identity unit of work's session, as a deletion uses it.
    async with IdentityUnitOfWork(create_session_factory(engine)) as uow:
        return await uow.ownership.owns_any_community(UserId(user_id))


async def test_owns_any_community_true_for_owner(engine: AsyncEngine) -> None:
    owner_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _provision(engine)(name="guild", owner_user_id=CommunityUserId(owner_id))

    assert await _owns_any_community(engine, owner_id) is True


async def test_owns_any_community_false_for_plain_member(engine: AsyncEngine) -> None:
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, member_id, "bob")
    community = await _provision(engine)(
        name="guild", owner_user_id=CommunityUserId(owner_id)
    )
    # A member without the Owner role must not be reported as an owner.
    await _add_member(engine)(
        community_id=community.id, user_id=CommunityUserId(member_id)
    )

    assert await _owns_any_community(engine, member_id) is False


async def test_owns_any_community_false_for_a_member_holding_another_role(
    engine: AsyncEngine,
) -> None:
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, member_id, "bob")
    community = await _provision(engine)(
        name="guild", owner_user_id=CommunityUserId(owner_id)
    )
    await _add_member(engine)(
        community_id=community.id, user_id=CommunityUserId(member_id)
    )
    factory = create_session_factory(engine)
    role = await CreateRole(uow=CommunityUnitOfWork(factory), clock=SystemClock())(
        community_id=community.id,
        actor_id=CommunityUserId(owner_id),
        name="Moderators",
        permissions={Permission("member:read")},
    )
    await AssignRole(uow=CommunityUnitOfWork(factory))(
        community_id=community.id,
        user_id=CommunityUserId(member_id),
        role_id=role.id,
        actor_id=CommunityUserId(owner_id),
    )

    # Only the preset Owner role makes an owner, not any role held.
    assert await _owns_any_community(engine, member_id) is False


async def test_owns_any_community_false_for_non_member(engine: AsyncEngine) -> None:
    owner_id = uuid.uuid4()
    stranger_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, stranger_id, "carol")
    await _provision(engine)(name="guild", owner_user_id=CommunityUserId(owner_id))

    assert await _owns_any_community(engine, stranger_id) is False


async def test_ownership_is_one_read_under_the_lock(engine: AsyncEngine) -> None:
    # A deletion reads ownership while it holds every active admin's user row
    # (#3217), so the read is one statement however many memberships there are.
    owner_id = uuid.uuid4()
    member_id = uuid.uuid4()
    await _insert_user(engine, owner_id, "alice")
    await _insert_user(engine, member_id, "bob")
    for name in ("guild", "outpost"):
        community = await _provision(engine)(
            name=name, owner_user_id=CommunityUserId(owner_id)
        )
        await _add_member(engine)(
            community_id=community.id, user_id=CommunityUserId(member_id)
        )
    statements: list[str] = []

    def _capture(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    async with IdentityUnitOfWork(create_session_factory(engine)) as uow:
        event.listen(engine.sync_engine, "before_cursor_execute", _capture)
        try:
            owns = await uow.ownership.owns_any_community(UserId(member_id))
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert owns is False
    locks = [s for s in statements if "pg_advisory_xact_lock" in s]
    reads = [s for s in statements if "pg_advisory_xact_lock" not in s]
    assert len(locks) == 1
    assert len(reads) == 1, reads
