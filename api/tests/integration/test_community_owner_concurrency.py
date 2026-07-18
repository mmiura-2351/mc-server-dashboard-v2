"""Concurrency test for the at-least-one-Owner invariant (#1959).

Proves the FOR UPDATE lock in ``lock_owner_role_holders`` closes the
last-two-owner race: two concurrent transactions each remove one of the only two
Owner-holding members. Without the lock both would see the other owner still
present, pass the guard, and commit -- leaving zero Owners. With it, the two
transactions serialize on the locked membership_role rows and exactly one
succeeds; the other re-counts the decremented set and refuses with
:class:`LastOwnerRemovalError`.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5), mirroring
``test_identity_last_admin_concurrency.py``.
"""

from __future__ import annotations

import asyncio
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
    RemoveMember,
    UnassignRole,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.errors import LastOwnerRemovalError
from mc_server_dashboard_api.community.domain.value_objects import UserId
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


async def test_concurrent_last_two_owner_removals_leave_exactly_one(
    engine: AsyncEngine,
) -> None:
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    await _insert_user(engine, owner_a, "owner_a")
    await _insert_user(engine, owner_b, "owner_b")

    factory = create_session_factory(engine)
    community = await ProvisionCommunity(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )(name="guild", owner_user_id=UserId(owner_a))

    # Add owner_b and assign them the Owner role.
    await AddMember(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )(community_id=community.id, user_id=UserId(owner_b))

    async with SqlAlchemyUnitOfWork(factory) as uow:
        roles = await uow.roles.list_for_community(community.id)
        owner_role = next(r for r in roles if r.is_preset)
        membership_b = await uow.memberships.get_by_user_and_community(
            UserId(owner_b), community.id
        )
        assert membership_b is not None
        await uow.memberships.assign_role(membership_b.id, owner_role.id)
        await uow.commit()

    async def remove(target: uuid.UUID) -> bool:
        # A fresh UnitOfWork == a fresh session/transaction per racer, matching
        # how two concurrent requests would run.
        try:
            await RemoveMember(uow=SqlAlchemyUnitOfWork(factory))(
                community_id=community.id, user_id=UserId(target)
            )
        except LastOwnerRemovalError:
            return False
        return True

    results = await asyncio.gather(remove(owner_a), remove(owner_b))

    # Exactly one of the two concurrent removals wins; the other is refused.
    assert sorted(results) == [False, True]

    async with SqlAlchemyUnitOfWork(factory) as uow:
        remaining = await uow.memberships.list_for_community(community.id)
    assert len(remaining) == 1


async def test_concurrent_last_two_owner_unassignments_leave_exactly_one(
    engine: AsyncEngine,
) -> None:
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    await _insert_user(engine, owner_a, "owner_a")
    await _insert_user(engine, owner_b, "owner_b")

    factory = create_session_factory(engine)
    community = await ProvisionCommunity(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )(name="guild", owner_user_id=UserId(owner_a))

    await AddMember(
        uow=SqlAlchemyUnitOfWork(factory),
        users=IdentityUserDirectory(IdentityUnitOfWork(factory)),
        clock=SystemClock(),
    )(community_id=community.id, user_id=UserId(owner_b))

    async with SqlAlchemyUnitOfWork(factory) as uow:
        roles = await uow.roles.list_for_community(community.id)
        owner_role = next(r for r in roles if r.is_preset)
        membership_b = await uow.memberships.get_by_user_and_community(
            UserId(owner_b), community.id
        )
        assert membership_b is not None
        await uow.memberships.assign_role(membership_b.id, owner_role.id)
        await uow.commit()

    owner_role_id = owner_role.id

    async def unassign(target: uuid.UUID) -> bool:
        try:
            await UnassignRole(uow=SqlAlchemyUnitOfWork(factory))(
                community_id=community.id,
                user_id=UserId(target),
                role_id=owner_role_id,
            )
        except LastOwnerRemovalError:
            return False
        return True

    results = await asyncio.gather(unassign(owner_a), unassign(owner_b))

    assert sorted(results) == [False, True]

    # Exactly one member still holds the Owner role.
    async with SqlAlchemyUnitOfWork(factory) as uow:
        members = await uow.memberships.list_for_community(community.id)
        owner_count = 0
        for m in members:
            held = await uow.memberships.list_role_ids(m.id)
            if owner_role_id in held:
                owner_count += 1
    assert owner_count == 1
