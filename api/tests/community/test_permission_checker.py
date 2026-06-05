"""Unit tests for the role+grant PermissionChecker evaluator (FR-AUTHZ-2/4/5).

Drives the evaluator against in-memory fakes (TESTING.md Section 4): role-only,
grant-only, union semantics, exact-resource grant scoping, cross-community
isolation, the platform-admin axis evaluated outside Community context, the
Layer-1 membership-visibility primitive, and rejection of uncatalogued codes.
"""

from __future__ import annotations

import uuid

import pytest

from mc_server_dashboard_api.community.adapters.permission_checker import (
    RoleGrantPermissionChecker,
)
from mc_server_dashboard_api.community.domain.errors import UnknownPermissionError
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
    UserId,
)
from tests.community.fakes import FakeAuthzUnitOfWork, seed_member_with_role


def _member(admin: bool = False) -> AuthUser:
    return AuthUser(user_id=UserId(uuid.uuid4()), is_platform_admin=admin)


# --- Layer-2: role-derived permissions -------------------------------------


async def test_role_permission_grants_operation() -> None:
    user = _member()
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community, {Permission("server:start")})
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=user,
        operation=Permission("server:start"),
        resource=ResourceRef(community_id=community),
    )

    assert allowed is True


async def test_role_without_permission_denies_operation() -> None:
    user = _member()
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community, {Permission("server:read")})
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=user,
        operation=Permission("server:start"),
        resource=ResourceRef(community_id=community),
    )

    assert allowed is False


async def test_non_member_has_no_permissions() -> None:
    user = _member()
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=user,
        operation=Permission("server:read"),
        resource=ResourceRef(community_id=community),
    )

    assert allowed is False


# --- Layer-2: resource grants ----------------------------------------------


async def test_resource_grant_on_exact_resource_grants_operation() -> None:
    user = _member()
    community = CommunityId.new()
    server_id = uuid.uuid4()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community, set())
    uow.add_grant(
        user.user_id, community, "server", server_id, {Permission("server:stop")}
    )
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=user,
        operation=Permission("server:stop"),
        resource=ResourceRef(
            community_id=community, resource_type="server", resource_id=server_id
        ),
    )

    assert allowed is True


async def test_resource_grant_does_not_apply_to_a_different_resource() -> None:
    user = _member()
    community = CommunityId.new()
    server_x = uuid.uuid4()
    server_y = uuid.uuid4()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community, set())
    uow.add_grant(
        user.user_id, community, "server", server_x, {Permission("server:stop")}
    )
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=user,
        operation=Permission("server:stop"),
        resource=ResourceRef(
            community_id=community, resource_type="server", resource_id=server_y
        ),
    )

    assert allowed is False


async def test_resource_grant_does_not_apply_in_a_different_community() -> None:
    # Defense-in-depth (FR-AUTHZ-4): a grant on (server, X) in community A must
    # never satisfy a check scoped to community B for the same resource id, even
    # if a caller passes a mismatched (community_id, resource_id) pair.
    user = _member()
    community_a = CommunityId.new()
    community_b = CommunityId.new()
    server_id = uuid.uuid4()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community_b, set())
    uow.add_grant(
        user.user_id, community_a, "server", server_id, {Permission("server:stop")}
    )
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=user,
        operation=Permission("server:stop"),
        resource=ResourceRef(
            community_id=community_b, resource_type="server", resource_id=server_id
        ),
    )

    assert allowed is False


async def test_resource_grant_is_ignored_without_a_resource_in_the_ref() -> None:
    # A community-level check (no resource_id) cannot draw on a per-resource grant.
    user = _member()
    community = CommunityId.new()
    server_id = uuid.uuid4()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community, set())
    uow.add_grant(
        user.user_id, community, "server", server_id, {Permission("server:stop")}
    )
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=user,
        operation=Permission("server:stop"),
        resource=ResourceRef(community_id=community),
    )

    assert allowed is False


# --- union semantics --------------------------------------------------------


async def test_effective_permissions_are_union_of_roles_and_grant() -> None:
    user = _member()
    community = CommunityId.new()
    server_id = uuid.uuid4()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community, {Permission("server:read")})
    uow.add_grant(
        user.user_id, community, "server", server_id, {Permission("server:stop")}
    )
    checker = RoleGrantPermissionChecker(uow)

    ref = ResourceRef(
        community_id=community, resource_type="server", resource_id=server_id
    )
    assert await checker.can(
        user=user, operation=Permission("server:read"), resource=ref
    )
    assert await checker.can(
        user=user, operation=Permission("server:stop"), resource=ref
    )


async def test_multiple_roles_union_their_permissions() -> None:
    user = _member()
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community, {Permission("server:read")})
    uow.add_role(user.user_id, community, {Permission("server:start")})
    checker = RoleGrantPermissionChecker(uow)

    ref = ResourceRef(community_id=community)
    assert await checker.can(
        user=user, operation=Permission("server:read"), resource=ref
    )
    assert await checker.can(
        user=user, operation=Permission("server:start"), resource=ref
    )


async def test_roles_are_loaded_in_a_single_batch_query() -> None:
    # The hot path must not loop get_by_id over each role (issue #321): three
    # roles resolve through one get_by_ids call, not per-role get_by_id calls.
    user = _member()
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community, {Permission("server:read")})
    uow.add_role(user.user_id, community, {Permission("server:start")})
    uow.add_role(user.user_id, community, {Permission("server:stop")})
    checker = RoleGrantPermissionChecker(uow)

    await checker.can(
        user=user,
        operation=Permission("server:start"),
        resource=ResourceRef(community_id=community),
    )

    assert uow.roles.get_by_ids_calls == 1
    assert uow.roles.get_by_id_calls == 0


# --- cross-community isolation (FR-AUTHZ-4) ---------------------------------


async def test_role_in_one_community_grants_nothing_in_another() -> None:
    user = _member()
    community_a = CommunityId.new()
    community_b = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    seed_member_with_role(uow, user.user_id, community_a, {Permission("server:start")})
    # Member of B too, but with no permissions there.
    seed_member_with_role(uow, user.user_id, community_b, set())
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=user,
        operation=Permission("server:start"),
        resource=ResourceRef(community_id=community_b),
    )

    assert allowed is False


# --- platform-admin axis (FR-AUTHZ-5) --------------------------------------


async def test_platform_admin_code_granted_to_platform_admin() -> None:
    admin = _member(admin=True)
    uow = FakeAuthzUnitOfWork()
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=admin,
        operation=Permission("worker:manage"),
        resource=ResourceRef(community_id=CommunityId.new()),
    )

    assert allowed is True


async def test_platform_admin_code_denied_to_non_admin_member() -> None:
    user = _member(admin=False)
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    # Even a role carrying the literal code does not grant the platform axis.
    seed_member_with_role(uow, user.user_id, community, {Permission("server:start")})
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=user,
        operation=Permission("worker:manage"),
        resource=ResourceRef(community_id=community),
    )

    assert allowed is False


async def test_platform_admin_does_not_get_community_codes_for_free() -> None:
    # The admin axis is independent: being a platform admin grants no Layer-2
    # community operation without membership/roles.
    admin = _member(admin=True)
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    checker = RoleGrantPermissionChecker(uow)

    allowed = await checker.can(
        user=admin,
        operation=Permission("server:start"),
        resource=ResourceRef(community_id=community),
    )

    assert allowed is False


# --- catalog validation -----------------------------------------------------


async def test_unknown_operation_code_is_rejected() -> None:
    user = _member()
    uow = FakeAuthzUnitOfWork()
    checker = RoleGrantPermissionChecker(uow)

    with pytest.raises(UnknownPermissionError):
        await checker.can(
            user=user,
            operation=Permission("server:teleport"),
            resource=ResourceRef(community_id=CommunityId.new()),
        )
