"""Unit tests for the effective-permissions read use case (issue #354).

Drives :class:`ReadMyEffectivePermissions` against in-memory fakes (TESTING.md
Section 4). The use case must read the SAME stores the ``PermissionChecker``
reads so it cannot drift from enforcement: the community-wide ``permissions`` is
the union of the member's role permission sets, and ``grants`` mirrors the
caller's own resource grants. The platform-admin flag carries no community-scoped
bypass, so an admin member is computed exactly like any other member.
"""

from __future__ import annotations

import uuid

from mc_server_dashboard_api.community.adapters.permission_checker import (
    RoleGrantPermissionChecker,
)
from mc_server_dashboard_api.community.application.read_my_permissions import (
    EffectivePermissions,
    ReadMyEffectivePermissions,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
    UserId,
)
from tests.community.fakes import FakeAuthzUnitOfWork


def _member(admin: bool = False) -> AuthUser:
    return AuthUser(user_id=UserId(uuid.uuid4()), is_platform_admin=admin)


async def test_returns_role_permission_union() -> None:
    user = _member()
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    uow.add_role(user.user_id, community, {Permission("server:read")})
    uow.add_role(user.user_id, community, {Permission("file:read")})
    use_case = ReadMyEffectivePermissions(uow=uow)

    result = await use_case(user=user, community_id=community)

    assert isinstance(result, EffectivePermissions)
    assert result.permissions == {Permission("server:read"), Permission("file:read")}
    assert result.grants == []


async def test_member_without_role_has_empty_permissions() -> None:
    user = _member()
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    uow.add_role(user.user_id, community, set())
    use_case = ReadMyEffectivePermissions(uow=uow)

    result = await use_case(user=user, community_id=community)

    assert result.permissions == set()
    assert result.grants == []


async def test_returns_only_own_grants() -> None:
    user = _member()
    other = _member()
    community = CommunityId.new()
    server_id = uuid.uuid4()
    uow = FakeAuthzUnitOfWork()
    uow.add_role(user.user_id, community, {Permission("server:read")})
    uow.add_grant(
        user.user_id,
        community,
        "server",
        server_id,
        {Permission("server:start"), Permission("server:stop")},
    )
    # Another member's grant in the same community must not leak.
    uow.add_grant(
        other.user_id, community, "server", uuid.uuid4(), {Permission("server:start")}
    )
    use_case = ReadMyEffectivePermissions(uow=uow)

    result = await use_case(user=user, community_id=community)

    assert result.permissions == {Permission("server:read")}
    assert len(result.grants) == 1
    grant = result.grants[0]
    assert grant.resource_type == "server"
    assert grant.resource_id == server_id
    assert grant.permissions == {Permission("server:start"), Permission("server:stop")}


async def test_grant_only_member_sees_grant() -> None:
    user = _member()
    community = CommunityId.new()
    server_id = uuid.uuid4()
    uow = FakeAuthzUnitOfWork()
    # Member with no role, only a resource grant.
    uow.add_role(user.user_id, community, set())
    uow.add_grant(
        user.user_id, community, "server", server_id, {Permission("server:start")}
    )
    use_case = ReadMyEffectivePermissions(uow=uow)

    result = await use_case(user=user, community_id=community)

    assert result.permissions == set()
    assert len(result.grants) == 1
    assert result.grants[0].permissions == {Permission("server:start")}


async def test_platform_admin_gets_plain_role_union_not_catalog() -> None:
    # The platform-admin flag carries no community-scoped bypass in enforcement
    # (RoleGrantPermissionChecker.can only bypasses on the platform-admin axis),
    # so an admin member is computed exactly like any other member: the union of
    # their role permission sets, never the full catalog.
    admin = _member(admin=True)
    community = CommunityId.new()
    uow = FakeAuthzUnitOfWork()
    uow.add_role(admin.user_id, community, {Permission("server:read")})
    use_case = ReadMyEffectivePermissions(uow=uow)

    result = await use_case(user=admin, community_id=community)

    assert result.permissions == {Permission("server:read")}
    assert result.grants == []

    # No drift from enforcement: a community code the admin does not hold via a
    # role is reported as absent here AND denied by the checker.
    assert Permission("server:start") not in result.permissions
    checker = RoleGrantPermissionChecker(uow)
    enforced = await checker.can(
        user=admin,
        operation=Permission("server:start"),
        resource=ResourceRef(community_id=community),
    )
    assert enforced is False
