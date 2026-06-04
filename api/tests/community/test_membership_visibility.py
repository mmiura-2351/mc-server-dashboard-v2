"""Unit tests for the Layer-1 membership-visibility primitive (FR-COMM-3, Section 6.4).

A non-member must get no existence signal; the route layer turns a ``False``
here into a 404 before any Layer-2 permission evaluation.
"""

from __future__ import annotations

import uuid

from mc_server_dashboard_api.community.adapters.permission_checker import (
    RepositoryMembershipVisibility,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityId, UserId
from tests.community.fakes import FakeAuthzUnitOfWork, seed_member_with_role


async def test_member_is_visible() -> None:
    user_id = UserId(uuid.uuid4())
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user_id, community, set())
    visibility = RepositoryMembershipVisibility(uow)

    assert await visibility.is_member(user_id=user_id, community_id=community) is True


async def test_non_member_is_invisible() -> None:
    user_id = UserId(uuid.uuid4())
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    visibility = RepositoryMembershipVisibility(uow)

    assert await visibility.is_member(user_id=user_id, community_id=community) is False


async def test_membership_in_another_community_is_not_visibility() -> None:
    user_id = UserId(uuid.uuid4())
    community_a = CommunityId.new()
    community_b = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user_id, community_a, set())
    visibility = RepositoryMembershipVisibility(uow)

    assert (
        await visibility.is_member(user_id=user_id, community_id=community_b) is False
    )
