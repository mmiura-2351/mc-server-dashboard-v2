"""HTTP edge for community resource-grant management (Section 6.4).

Routes live flat under ``/communities/{community_id}/grants`` rather than nested
under a member. A grant is a community-scoped entity keyed by
``(user, community, resource_type, resource_id)`` and gated by the *community-level*
``grant:read`` / ``grant:manage`` permissions, not by a per-member permission, so
nesting it under ``/members/{user_id}`` would put a non-key segment in the path and
diverge from the gate's scope. Flat ``/grants`` mirrors the ``/members`` sibling
shape and keeps ``community_id`` as the path param ``require_permission`` reads. The
list route accepts an optional ``?user_id=`` filter for the per-member view.

The router is thin: it resolves use cases via dependency injection, runs them, and
serialises the result. Domain errors are translated to HTTP codes here.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.application.manage_grant import (
    CreateGrant,
    ListGrants,
    RevokeGrant,
)
from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.errors import (
    GrantResourceNotFoundError,
    GrantTargetNotMemberError,
    InvalidGrantResourceTypeError,
    InvalidPermissionError,
    ResourceGrantAlreadyExistsError,
    ResourceGrantNotFoundError,
    UnknownPermissionError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceGrantId,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_create_grant,
    get_list_grants,
    get_revoke_grant,
    require_permission,
)

router = APIRouter()


class CreateGrantRequest(BaseModel):
    user_id: str = Field(min_length=1)
    resource_type: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    permissions: list[str]


class GrantResponse(BaseModel):
    """Public view of a resource grant (DATABASE.md Section 6)."""

    id: str
    user_id: str
    resource_type: str
    resource_id: str
    permissions: list[str]

    @classmethod
    def from_entity(cls, grant: ResourceGrant) -> "GrantResponse":
        return cls(
            id=str(grant.id.value),
            user_id=str(grant.user_id.value),
            resource_type=grant.resource_type,
            resource_id=str(grant.resource_id),
            permissions=sorted(perm.value for perm in grant.permissions),
        )


@router.get("/communities/{community_id}/grants")
async def list_grants(
    community_id: uuid.UUID,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("grant:read")))
    ],
    use_case: Annotated[ListGrants, Depends(get_list_grants)],
    user_id: uuid.UUID | None = None,
) -> list[GrantResponse]:
    grants = await use_case(
        community_id=CommunityId(community_id),
        user_id=None if user_id is None else UserId(user_id),
    )
    return [GrantResponse.from_entity(grant) for grant in grants]


@router.post(
    "/communities/{community_id}/grants",
    status_code=status.HTTP_201_CREATED,
)
async def create_grant(
    community_id: uuid.UUID,
    body: CreateGrantRequest,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("grant:manage")))
    ],
    use_case: Annotated[CreateGrant, Depends(get_create_grant)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> GrantResponse:
    try:
        grant = await use_case(
            community_id=CommunityId(community_id),
            user_id=_parse_user_id(body.user_id),
            resource_type=body.resource_type,
            resource_id=_parse_resource_id(body.resource_id),
            permissions=_parse_permissions(body.permissions),
        )
    except GrantTargetNotMemberError as exc:
        # No existence signal about who is or is not a member (Section 6.4): a
        # non-member target is reported as a 404, not a distinct 422.
        raise _not_found() from exc
    except GrantResourceNotFoundError as exc:
        # A grant on a resource that does not exist in the community is rejected as
        # not-found (issue #361), the same no-existence-signal posture.
        raise _not_found() from exc
    except InvalidGrantResourceTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "invalid_resource_type"},
        ) from exc
    except UnknownPermissionError as exc:
        raise _invalid_permission() from exc
    except ResourceGrantAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "grant_exists"},
        ) from exc
    await recorder.record(
        AuditEvent(
            operation=ops.GRANT_CREATE,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_GRANT,
            target_id=grant.id.value,
        )
    )
    return GrantResponse.from_entity(grant)


@router.delete(
    "/communities/{community_id}/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_grant(
    community_id: uuid.UUID,
    grant_id: uuid.UUID,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("grant:manage")))
    ],
    use_case: Annotated[RevokeGrant, Depends(get_revoke_grant)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    try:
        await use_case(
            community_id=CommunityId(community_id),
            grant_id=ResourceGrantId(grant_id),
        )
    except ResourceGrantNotFoundError as exc:
        raise _not_found() from exc
    await recorder.record(
        AuditEvent(
            operation=ops.GRANT_REVOKE,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_GRANT,
            target_id=grant_id,
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


def _parse_resource_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"reason": "invalid_resource_id"},
        ) from exc


def _parse_permissions(raw: list[str]) -> set[Permission]:
    # Shape (<resource>:<action>) only; catalog and resource-type validation are
    # the use case's job, surfacing as UnknownPermissionError.
    try:
        return {Permission(code) for code in raw}
    except InvalidPermissionError as exc:
        raise _invalid_permission() from exc


def _invalid_permission() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"reason": "invalid_permission"},
    )


def _not_found() -> HTTPException:
    # Keep the no-existence-signal posture (Section 6.4): a grant outside this
    # community or a non-member target both 404.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
