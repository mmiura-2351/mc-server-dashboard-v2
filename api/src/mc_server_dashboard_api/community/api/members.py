"""HTTP edge for community membership management (Section 6.3, 6.4).

Routes live under ``/communities/{community_id}/members`` per the established
convention so ``require_permission`` can read ``community_id`` from the path and
apply the two-layer check (non-member -> 404 with no existence signal,
member-without-permission -> 403; Section 6.4). The router is thin: it resolves
use cases via dependency injection, runs them, and serialises the result.

Permission gating per operation:

- add (``member:add``) / remove (``member:remove``) / list (``member:read``).
- role assign/unassign use ``role:manage``: assigning a role changes what a
  member can *do*, so it is a role-management operation, not merely member-add.

Domain errors are translated to HTTP codes here.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.application.manage_membership import (
    AddMember,
    AssignRole,
    ListMembers,
    MemberView,
    RemoveMember,
    UnassignRole,
)
from mc_server_dashboard_api.community.domain.entities import Membership
from mc_server_dashboard_api.community.domain.errors import (
    CommunityNotFoundError,
    LastOwnerRemovalError,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    MemberUserNotFoundError,
    RoleNotFoundError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    RoleId,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_add_member,
    get_assign_role,
    get_audit_recorder,
    get_list_members,
    get_remove_member,
    get_unassign_role,
    require_permission,
)

router = APIRouter()


class AddMemberRequest(BaseModel):
    user_id: str = Field(min_length=1)


class AssignRoleRequest(BaseModel):
    role_id: str = Field(min_length=1)


class MembershipResponse(BaseModel):
    """Public view of a membership (the (user, community) join, DATABASE.md 5)."""

    membership_id: str
    user_id: str

    @classmethod
    def from_entity(cls, membership: Membership) -> "MembershipResponse":
        return cls(
            membership_id=str(membership.id.value),
            user_id=str(membership.user_id.value),
        )


class MemberResponse(BaseModel):
    """A member of a community with the names of the roles they hold."""

    membership_id: str
    user_id: str
    role_names: list[str]

    @classmethod
    def from_view(cls, view: MemberView) -> "MemberResponse":
        return cls(
            membership_id=str(view.membership_id.value),
            user_id=str(view.user_id.value),
            role_names=view.role_names,
        )


@router.post(
    "/communities/{community_id}/members",
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    community_id: uuid.UUID,
    body: AddMemberRequest,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("member:add")))
    ],
    use_case: Annotated[AddMember, Depends(get_add_member)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> MembershipResponse:
    try:
        membership = await use_case(
            community_id=CommunityId(community_id),
            user_id=_parse_user_id(body.user_id),
        )
    except MemberUserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "user_not_found"},
        ) from exc
    except MembershipAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "already_member"},
        ) from exc
    await recorder.record(
        AuditEvent(
            operation=ops.MEMBER_ADD,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_USER,
            target_id=membership.user_id.value,
        )
    )
    return MembershipResponse.from_entity(membership)


@router.get("/communities/{community_id}/members")
async def list_members(
    community_id: uuid.UUID,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("member:read")))
    ],
    use_case: Annotated[ListMembers, Depends(get_list_members)],
) -> list[MemberResponse]:
    try:
        members = await use_case(community_id=CommunityId(community_id))
    except CommunityNotFoundError as exc:
        raise _not_found() from exc
    return [MemberResponse.from_view(view) for view in members]


@router.delete(
    "/communities/{community_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    community_id: uuid.UUID,
    user_id: uuid.UUID,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("member:remove")))
    ],
    use_case: Annotated[RemoveMember, Depends(get_remove_member)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    try:
        await use_case(community_id=CommunityId(community_id), user_id=UserId(user_id))
    except MembershipNotFoundError as exc:
        raise _not_found() from exc
    except LastOwnerRemovalError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "last_owner"},
        ) from exc
    await recorder.record(
        AuditEvent(
            operation=ops.MEMBER_REMOVE,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_USER,
            target_id=user_id,
        )
    )


@router.post(
    "/communities/{community_id}/members/{user_id}/roles",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def assign_role(
    community_id: uuid.UUID,
    user_id: uuid.UUID,
    body: AssignRoleRequest,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("role:manage")))
    ],
    use_case: Annotated[AssignRole, Depends(get_assign_role)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    role_id = _parse_role_id(body.role_id)
    try:
        await use_case(
            community_id=CommunityId(community_id),
            user_id=UserId(user_id),
            role_id=role_id,
        )
    except MembershipNotFoundError as exc:
        raise _not_found() from exc
    except RoleNotFoundError as exc:
        raise _not_found() from exc
    await recorder.record(
        AuditEvent(
            operation=ops.ROLE_ASSIGN,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_USER,
            target_id=user_id,
        )
    )


@router.delete(
    "/communities/{community_id}/members/{user_id}/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unassign_role(
    community_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("role:manage")))
    ],
    use_case: Annotated[UnassignRole, Depends(get_unassign_role)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    try:
        await use_case(
            community_id=CommunityId(community_id),
            user_id=UserId(user_id),
            role_id=RoleId(role_id),
        )
    except MembershipNotFoundError as exc:
        raise _not_found() from exc
    except RoleNotFoundError as exc:
        raise _not_found() from exc
    except LastOwnerRemovalError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "last_owner"},
        ) from exc
    await recorder.record(
        AuditEvent(
            operation=ops.ROLE_UNASSIGN,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_USER,
            target_id=user_id,
        )
    )


def _parse_user_id(raw: str) -> UserId:
    try:
        return UserId(uuid.UUID(raw))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "invalid_user_id"},
        ) from exc


def _parse_role_id(raw: str) -> RoleId:
    try:
        return RoleId(uuid.UUID(raw))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "invalid_role_id"},
        ) from exc


def _not_found() -> HTTPException:
    # Keep the no-existence-signal posture (Section 6.4): a missing member, a role
    # outside this community, or a community that vanished concurrently all 404.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
