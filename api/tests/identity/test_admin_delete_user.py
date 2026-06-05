"""Unit tests for the AdminDeleteUser use case (admin delete, issue #278).

Mirrors the self-service DeleteAccount refusals (community owner, last active
admin) and adds the admin-route guard that an admin cannot delete themselves
through this route. Drives the use case against in-memory fakes (TESTING.md
Section 4).
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.admin_delete_user import (
    AdminDeleteUser,
)
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.errors import (
    CommunityOwnedError,
    LastPlatformAdminError,
    SelfTargetError,
    UserNotFoundError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    UserId,
)
from tests.identity.fakes import (
    FakeClock,
    FakeCommunityOwnership,
    FakeUnitOfWork,
    make_user,
)

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _use_case(
    uow: FakeUnitOfWork, ownership: FakeCommunityOwnership
) -> AdminDeleteUser:
    return AdminDeleteUser(uow=uow, ownership=ownership, clock=FakeClock(_NOW))


def _active_token(user_id: UserId) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id,
        token_hash=f"hash::{user_id.value}",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=14),
    )


async def test_deletes_target_and_revokes_tokens() -> None:
    admin = make_user(username="admin", is_platform_admin=True)
    target = make_user(username="bob", email="bob@example.com")
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(target)
    uow.refresh_tokens.seed(_active_token(target.id))

    await _use_case(uow, FakeCommunityOwnership())(
        actor_id=admin.id, target_id=target.id
    )

    assert target.id not in uow.users.by_id
    assert all(t.revoked_at == _NOW for t in uow.refresh_tokens.by_hash.values())
    assert uow.commits == 1


async def test_delete_self_refused() -> None:
    admin = make_user(username="admin", is_platform_admin=True)
    keep = make_user(username="keep", email="keep@example.com", is_platform_admin=True)
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(keep)

    with pytest.raises(SelfTargetError):
        await _use_case(uow, FakeCommunityOwnership())(
            actor_id=admin.id, target_id=admin.id
        )
    assert admin.id in uow.users.by_id
    assert uow.commits == 0


async def test_delete_community_owner_refused() -> None:
    admin = make_user(username="admin", is_platform_admin=True)
    target = make_user(username="owner", email="owner@example.com")
    uow = FakeUnitOfWork()
    uow.users.seed(admin)
    uow.users.seed(target)
    ownership = FakeCommunityOwnership(owners={target.id})

    with pytest.raises(CommunityOwnedError):
        await _use_case(uow, ownership)(actor_id=admin.id, target_id=target.id)
    assert target.id in uow.users.by_id
    assert uow.commits == 0


async def test_delete_last_active_admin_refused() -> None:
    actor = make_user(username="actor", email="actor@example.com")  # non-admin actor
    target = make_user(
        username="solo", email="solo@example.com", is_platform_admin=True
    )
    uow = FakeUnitOfWork()
    uow.users.seed(actor)
    uow.users.seed(target)

    with pytest.raises(LastPlatformAdminError):
        await _use_case(uow, FakeCommunityOwnership())(
            actor_id=actor.id, target_id=target.id
        )
    assert target.id in uow.users.by_id
    assert uow.commits == 0


async def test_delete_admin_allowed_when_other_active_admin_remains() -> None:
    actor = make_user(username="actor", is_platform_admin=True)
    target = make_user(username="t", email="t@example.com", is_platform_admin=True)
    uow = FakeUnitOfWork()
    uow.users.seed(actor)
    uow.users.seed(target)

    await _use_case(uow, FakeCommunityOwnership())(
        actor_id=actor.id, target_id=target.id
    )

    assert target.id not in uow.users.by_id


async def test_unknown_target_raises() -> None:
    admin = make_user(username="admin", is_platform_admin=True)
    uow = FakeUnitOfWork()
    uow.users.seed(admin)

    with pytest.raises(UserNotFoundError):
        await _use_case(uow, FakeCommunityOwnership())(
            actor_id=admin.id, target_id=UserId.new()
        )
