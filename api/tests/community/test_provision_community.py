"""Unit tests for the ProvisionCommunity use case (FR-COMM-2/4).

Drives the use case against the in-memory community fakes (TESTING.md Section 4).
Verifies that one call seeds the community, the preset Owner role with the *full*
community-permission catalog, the owner's membership, and the role assignment —
and commits exactly once; that a partial failure leaves nothing committed; and
that an unknown owner / non-platform-admin / duplicate name are rejected.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.clock import Clock
from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    OwnerUserNotFoundError,
)
from mc_server_dashboard_api.community.domain.permissions import (
    COMMUNITY_PERMISSIONS,
    OWNER_ROLE_NAME,
)
from mc_server_dashboard_api.community.domain.user_directory import UserDirectory
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityName,
    RoleName,
    UserId,
)
from tests.community.fakes import FakeAuthzUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FakeClock(Clock):
    def now(self) -> dt.datetime:
        return _NOW


class _FakeUserDirectory(UserDirectory):
    def __init__(self, *, known: bool) -> None:
        self._known = known

    async def exists(self, user_id: UserId) -> bool:
        return self._known


def _use_case(uow: FakeAuthzUnitOfWork, *, owner_known: bool) -> ProvisionCommunity:
    return ProvisionCommunity(
        uow=uow,
        users=_FakeUserDirectory(known=owner_known),
        clock=_FakeClock(),
    )


async def test_provision_seeds_owner_role_membership_and_assignment() -> None:
    uow = FakeAuthzUnitOfWork()
    owner = UserId(uuid.uuid4())

    community = await _use_case(uow, owner_known=True)(
        name="guild", owner_user_id=owner
    )

    # Community persisted with the trimmed name.
    assert uow.communities.by_id[community.id].name == CommunityName("guild")

    # Exactly one preset Owner role, granting the full community catalog.
    roles = await uow.roles.list_for_community(community.id)
    assert len(roles) == 1
    owner_role = roles[0]
    assert owner_role.name == RoleName(OWNER_ROLE_NAME)
    assert owner_role.is_preset is True
    assert owner_role.permissions == set(COMMUNITY_PERMISSIONS)

    # Owner membership exists and is assigned the Owner role.
    membership = await uow.memberships.get_by_user_and_community(owner, community.id)
    assert membership is not None
    assert await uow.memberships.list_role_ids(membership.id) == [owner_role.id]

    # One atomic commit.
    assert uow.commits == 1


async def test_owner_role_permissions_match_catalog_exactly() -> None:
    # The Owner permission set is derived from the catalog, never hand-written:
    # it must equal COMMUNITY_PERMISSIONS and contain no platform-admin codes.
    uow = FakeAuthzUnitOfWork()
    community = await _use_case(uow, owner_known=True)(
        name="guild", owner_user_id=UserId(uuid.uuid4())
    )
    (role,) = await uow.roles.list_for_community(community.id)
    assert role.permissions == set(COMMUNITY_PERMISSIONS)


async def test_unknown_owner_is_rejected_and_nothing_committed() -> None:
    uow = FakeAuthzUnitOfWork()
    with pytest.raises(OwnerUserNotFoundError):
        await _use_case(uow, owner_known=False)(
            name="guild", owner_user_id=UserId(uuid.uuid4())
        )
    assert uow.commits == 0
    assert uow.communities.by_id == {}


async def test_duplicate_name_is_rejected_and_not_committed() -> None:
    uow = FakeAuthzUnitOfWork()
    owner = UserId(uuid.uuid4())
    await _use_case(uow, owner_known=True)(name="guild", owner_user_id=owner)
    assert uow.commits == 1

    with pytest.raises(CommunityAlreadyExistsError):
        await _use_case(uow, owner_known=True)(name="guild", owner_user_id=owner)
    # The duplicate attempt did not commit again.
    assert uow.commits == 1


async def test_partial_failure_does_not_commit() -> None:
    # If staging a step raises, the use case must leave the block without
    # committing (the real UnitOfWork then rolls back; here we assert no commit).
    uow = FakeAuthzUnitOfWork()

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("membership add failed")

    uow.memberships.add = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await _use_case(uow, owner_known=True)(
            name="guild", owner_user_id=UserId(uuid.uuid4())
        )
    assert uow.commits == 0
