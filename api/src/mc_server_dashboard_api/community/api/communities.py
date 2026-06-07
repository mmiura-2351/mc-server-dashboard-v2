"""HTTP edge for community provisioning and management (Section 6.2, 6.4).

The router is thin: it resolves use cases via dependency injection, runs them,
and serialises the result. Authorization is enforced by the shared dependencies,
not here:

- ``POST /communities`` requires the platform-admin axis (``require_platform_admin``
  / ``community:provision``, FR-COMM-2/FR-AUTHZ-5). The admin axis governs
  *provisioning only*, not community internals.
- ``GET/PATCH /communities/{community_id}`` go through ``require_permission``,
  which applies the two-layer check (non-member -> 404 with no existence signal,
  member-without-permission -> 403; FR-COMM-3, Section 6.4). A platform admin who
  is not a member therefore gets 404 on these too — the admin axis does not pierce
  community isolation.
- ``DELETE /communities/{community_id}`` is the one exception: it passes
  ``allow_platform_admin=True``, so a platform admin can delete *any* community
  (member or not) to clean up an orphan, while a non-admin still goes through the
  two-layer check (issue #489; WEBUI_SPEC.md Section 3).
- ``GET /communities`` lists only the requesting user's communities (FR-MEM-4);
  the platform-admin all-communities listing is ``GET /admin/communities``
  (``admin_communities.py``).

Domain errors are translated to HTTP codes here.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.application.list_my_communities import (
    ListMyCommunities,
)
from mc_server_dashboard_api.community.application.manage_community import (
    DeleteCommunity,
    ReadCommunity,
    RenameCommunity,
)
from mc_server_dashboard_api.community.application.provision_community import (
    ProvisionCommunity,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    CommunityNotFoundError,
    InvalidCommunityNameError,
    OwnerUserNotFoundError,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_current_user,
    get_delete_community,
    get_list_my_communities,
    get_provision_community,
    get_read_community,
    get_rename_community,
    require_permission,
    require_platform_admin,
)
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.identity.domain.entities import User

router = APIRouter()


class ProvisionCommunityRequest(BaseModel):
    name: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)


class RenameCommunityRequest(BaseModel):
    name: str = Field(min_length=1)


class CommunityResponse(BaseModel):
    """Public view of a community (DATABASE.md Section 5; quotas unused in M1)."""

    id: str
    name: str

    @classmethod
    def from_entity(cls, community: Community) -> "CommunityResponse":
        return cls(id=str(community.id.value), name=community.name.value)


@router.post(
    "/communities",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_platform_admin)],
)
async def provision_community(
    body: ProvisionCommunityRequest,
    use_case: Annotated[ProvisionCommunity, Depends(get_provision_community)],
    user: Annotated[User, Depends(get_current_user)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> CommunityResponse:
    try:
        community = await use_case(
            name=body.name, owner_user_id=_parse_user_id(body.owner_user_id)
        )
    except OwnerUserNotFoundError as exc:
        await _record_provision_failure(recorder, user)
        raise problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "owner_not_found") from exc
    except CommunityAlreadyExistsError as exc:
        await _record_provision_failure(recorder, user)
        raise problem(status.HTTP_409_CONFLICT, "name_taken") from exc
    except InvalidCommunityNameError as exc:
        await _record_provision_failure(recorder, user)
        raise problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_name") from exc
    await recorder.record(
        AuditEvent(
            operation=ops.COMMUNITY_PROVISION,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            community_id=community.id.value,
            target_type=ops.TARGET_COMMUNITY,
            target_id=community.id.value,
        )
    )
    return CommunityResponse.from_entity(community)


@router.get("/communities")
async def list_my_communities(
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ListMyCommunities, Depends(get_list_my_communities)],
) -> list[CommunityResponse]:
    communities = await use_case(user_id=UserId(user.id.value))
    return [CommunityResponse.from_entity(c) for c in communities]


@router.get("/communities/{community_id}")
async def read_community(
    community_id: uuid.UUID,
    _authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("community:read")))
    ],
    use_case: Annotated[ReadCommunity, Depends(get_read_community)],
) -> CommunityResponse:
    try:
        community = await use_case(community_id=CommunityId(community_id))
    except CommunityNotFoundError as exc:
        raise _not_found() from exc
    return CommunityResponse.from_entity(community)


@router.patch("/communities/{community_id}")
async def rename_community(
    community_id: uuid.UUID,
    body: RenameCommunityRequest,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("community:update")))
    ],
    use_case: Annotated[RenameCommunity, Depends(get_rename_community)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> CommunityResponse:
    try:
        community = await use_case(
            community_id=CommunityId(community_id), name=body.name
        )
    except CommunityNotFoundError as exc:
        raise _not_found() from exc
    except CommunityAlreadyExistsError as exc:
        raise problem(status.HTTP_409_CONFLICT, "name_taken") from exc
    except InvalidCommunityNameError as exc:
        raise problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_name") from exc
    await recorder.record(
        AuditEvent(
            operation=ops.COMMUNITY_UPDATE,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community.id.value,
            target_type=ops.TARGET_COMMUNITY,
            target_id=community.id.value,
        )
    )
    return CommunityResponse.from_entity(community)


@router.delete(
    "/communities/{community_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_community(
    community_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("community:delete"), allow_platform_admin=True
            )
        ),
    ],
    use_case: Annotated[DeleteCommunity, Depends(get_delete_community)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    try:
        await use_case(community_id=CommunityId(community_id))
    except CommunityNotFoundError as exc:
        raise _not_found() from exc
    await recorder.record(
        AuditEvent(
            operation=ops.COMMUNITY_DELETE,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_COMMUNITY,
            target_id=community_id,
        )
    )


async def _record_provision_failure(recorder: AuditRecorder, user: User) -> None:
    """Record a refused community provisioning attempt (issue #131; FR-AUD-1).

    A platform-admin action that was rejected (unknown owner, name taken, invalid
    name) — security-relevant, so the trail shows the attempt. No community was
    created, so there is no target id; the row is attributed to the admin actor.
    """

    await recorder.record(
        AuditEvent(
            operation=ops.COMMUNITY_PROVISION,
            outcome=Outcome.DENIED,
            actor_id=user.id.value,
        )
    )


def _parse_user_id(raw: str) -> UserId:
    try:
        return UserId(uuid.UUID(raw))
    except ValueError as exc:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_owner_user_id"
        ) from exc


def _not_found() -> ProblemException:
    # A member with the permission whose community vanished concurrently: keep the
    # no-existence-signal posture (Section 6.4).
    return problem(status.HTTP_404_NOT_FOUND, "not_found")
