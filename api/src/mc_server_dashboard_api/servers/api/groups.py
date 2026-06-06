"""HTTP edge for player-group management (issue #276).

Routes live under ``/communities/{community_id}/groups`` (+ ``/{group_id}``), with
the player sub-collection at ``.../players`` and the server attachments at
``.../servers/{server_id}``. All routes are gated by the *community-level*
``group:read`` / ``group:manage`` permissions (a group is a community-scoped
entity, not per-server), so ``community_id`` is the path param
``require_permission`` reads.

The router is thin: it resolves use cases via dependency injection, runs them, and
serialises the result. Domain errors are translated to HTTP codes here, and group
mutations are audited (FR-AUD-1). Lives in its own module (not ``servers.py``) to
keep the new slice isolated.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder

# ``Permission`` is community-context-owned (the catalog lives there); the groups
# routes reference the ``group:*`` codes through it, as the servers routes do.
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_add_player,
    get_attach_group,
    get_audit_recorder,
    get_create_group,
    get_delete_group,
    get_detach_group,
    get_list_group_servers,
    get_list_groups,
    get_list_server_groups,
    get_read_group,
    get_remove_player,
    get_rename_group,
    require_permission,
)
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.servers.application.groups import (
    AddPlayer,
    AttachGroup,
    CreateGroup,
    DeleteGroup,
    DetachGroup,
    ListGroups,
    ListGroupServers,
    ListServerGroups,
    ReadGroup,
    RemovePlayer,
    RenameGroup,
)
from mc_server_dashboard_api.servers.domain.errors import (
    GroupAttachmentNotFoundError,
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    InvalidGroupKindError,
    InvalidGroupNameError,
    InvalidPlayerError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.groups import GroupId, PlayerGroup
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

router = APIRouter()


class CreateGroupRequest(BaseModel):
    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)


class RenameGroupRequest(BaseModel):
    name: str = Field(min_length=1)


class AddPlayerRequest(BaseModel):
    uuid: str = Field(min_length=1)
    username: str = Field(min_length=1)


class PlayerResponse(BaseModel):
    uuid: str
    username: str


class GroupResponse(BaseModel):
    """Public view of a player group (issue #276)."""

    id: str
    community_id: str
    name: str
    kind: str
    players: list[PlayerResponse]

    @classmethod
    def from_entity(cls, group: PlayerGroup) -> "GroupResponse":
        return cls(
            id=str(group.id.value),
            community_id=str(group.community_id.value),
            name=group.name.value,
            kind=group.kind.value,
            players=[
                PlayerResponse(uuid=str(p.uuid), username=p.username)
                for p in group.players
            ],
        )


@router.post(
    "/communities/{community_id}/groups",
    status_code=status.HTTP_201_CREATED,
)
async def create_group(
    community_id: uuid.UUID,
    body: CreateGroupRequest,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("group:manage")))
    ],
    use_case: Annotated[CreateGroup, Depends(get_create_group)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> GroupResponse:
    try:
        group = await use_case(
            community_id=CommunityId(community_id),
            name=body.name,
            kind=body.kind,
        )
    except InvalidGroupKindError as exc:
        raise _unprocessable("invalid_group_kind") from exc
    except InvalidGroupNameError as exc:
        raise _unprocessable("invalid_group_name") from exc
    except GroupNameAlreadyExistsError as exc:
        raise _conflict("group_name_exists") from exc
    await _record(recorder, ops.GROUP_CREATE, authorized, community_id, group.id.value)
    return GroupResponse.from_entity(group)


@router.get("/communities/{community_id}/groups")
async def list_groups(
    community_id: uuid.UUID,
    _authorized: Annotated[
        object, Depends(require_permission(Permission("group:read")))
    ],
    use_case: Annotated[ListGroups, Depends(get_list_groups)],
) -> list[GroupResponse]:
    groups = await use_case(community_id=CommunityId(community_id))
    return [GroupResponse.from_entity(group) for group in groups]


@router.get("/communities/{community_id}/groups/{group_id}")
async def read_group(
    community_id: uuid.UUID,
    group_id: uuid.UUID,
    _authorized: Annotated[
        object, Depends(require_permission(Permission("group:read")))
    ],
    use_case: Annotated[ReadGroup, Depends(get_read_group)],
) -> GroupResponse:
    try:
        group = await use_case(
            community_id=CommunityId(community_id), group_id=GroupId(group_id)
        )
    except GroupNotFoundError as exc:
        raise _not_found() from exc
    return GroupResponse.from_entity(group)


@router.patch("/communities/{community_id}/groups/{group_id}")
async def rename_group(
    community_id: uuid.UUID,
    group_id: uuid.UUID,
    body: RenameGroupRequest,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("group:manage")))
    ],
    use_case: Annotated[RenameGroup, Depends(get_rename_group)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> GroupResponse:
    try:
        group = await use_case(
            community_id=CommunityId(community_id),
            group_id=GroupId(group_id),
            name=body.name,
        )
    except GroupNotFoundError as exc:
        raise _not_found() from exc
    except InvalidGroupNameError as exc:
        raise _unprocessable("invalid_group_name") from exc
    except GroupNameAlreadyExistsError as exc:
        raise _conflict("group_name_exists") from exc
    await _record(recorder, ops.GROUP_UPDATE, authorized, community_id, group.id.value)
    return GroupResponse.from_entity(group)


@router.delete(
    "/communities/{community_id}/groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_group(
    community_id: uuid.UUID,
    group_id: uuid.UUID,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("group:manage")))
    ],
    use_case: Annotated[DeleteGroup, Depends(get_delete_group)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    try:
        await use_case(
            community_id=CommunityId(community_id), group_id=GroupId(group_id)
        )
    except GroupNotFoundError as exc:
        raise _not_found() from exc
    await _record(recorder, ops.GROUP_DELETE, authorized, community_id, group_id)


@router.post(
    "/communities/{community_id}/groups/{group_id}/players",
    status_code=status.HTTP_200_OK,
)
async def add_player(
    community_id: uuid.UUID,
    group_id: uuid.UUID,
    body: AddPlayerRequest,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("group:manage")))
    ],
    use_case: Annotated[AddPlayer, Depends(get_add_player)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> GroupResponse:
    player_uuid = _parse_uuid(body.uuid)
    try:
        group = await use_case(
            community_id=CommunityId(community_id),
            group_id=GroupId(group_id),
            player_uuid=player_uuid,
            username=body.username,
        )
    except GroupNotFoundError as exc:
        raise _not_found() from exc
    except InvalidPlayerError as exc:
        raise _unprocessable("invalid_player") from exc
    await _record(
        recorder, ops.GROUP_PLAYER_ADD, authorized, community_id, group.id.value
    )
    return GroupResponse.from_entity(group)


@router.delete(
    "/communities/{community_id}/groups/{group_id}/players/{player_uuid}",
    status_code=status.HTTP_200_OK,
)
async def remove_player(
    community_id: uuid.UUID,
    group_id: uuid.UUID,
    player_uuid: uuid.UUID,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("group:manage")))
    ],
    use_case: Annotated[RemovePlayer, Depends(get_remove_player)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> GroupResponse:
    try:
        group = await use_case(
            community_id=CommunityId(community_id),
            group_id=GroupId(group_id),
            player_uuid=player_uuid,
        )
    except GroupNotFoundError as exc:
        raise _not_found() from exc
    await _record(
        recorder, ops.GROUP_PLAYER_REMOVE, authorized, community_id, group.id.value
    )
    return GroupResponse.from_entity(group)


@router.put(
    "/communities/{community_id}/groups/{group_id}/servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def attach_group(
    community_id: uuid.UUID,
    group_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("group:manage")))
    ],
    use_case: Annotated[AttachGroup, Depends(get_attach_group)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    try:
        await use_case(
            community_id=CommunityId(community_id),
            group_id=GroupId(group_id),
            server_id=ServerId(server_id),
        )
    except (GroupNotFoundError, ServerNotFoundError) as exc:
        raise _not_found() from exc
    await _record(recorder, ops.GROUP_ATTACH, authorized, community_id, group_id)


@router.delete(
    "/communities/{community_id}/groups/{group_id}/servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def detach_group(
    community_id: uuid.UUID,
    group_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser, Depends(require_permission(Permission("group:manage")))
    ],
    use_case: Annotated[DetachGroup, Depends(get_detach_group)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    try:
        await use_case(
            community_id=CommunityId(community_id),
            group_id=GroupId(group_id),
            server_id=ServerId(server_id),
        )
    except (
        GroupNotFoundError,
        ServerNotFoundError,
        GroupAttachmentNotFoundError,
    ) as exc:
        raise _not_found() from exc
    await _record(recorder, ops.GROUP_DETACH, authorized, community_id, group_id)


@router.get("/communities/{community_id}/groups/{group_id}/servers")
async def list_group_servers(
    community_id: uuid.UUID,
    group_id: uuid.UUID,
    _authorized: Annotated[
        object, Depends(require_permission(Permission("group:read")))
    ],
    use_case: Annotated[ListGroupServers, Depends(get_list_group_servers)],
) -> list[str]:
    try:
        server_ids = await use_case(
            community_id=CommunityId(community_id), group_id=GroupId(group_id)
        )
    except GroupNotFoundError as exc:
        raise _not_found() from exc
    return [str(server_id.value) for server_id in server_ids]


@router.get("/communities/{community_id}/servers/{server_id}/groups")
async def list_server_groups(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _authorized: Annotated[
        object, Depends(require_permission(Permission("group:read")))
    ],
    use_case: Annotated[ListServerGroups, Depends(get_list_server_groups)],
) -> list[GroupResponse]:
    try:
        groups = await use_case(
            community_id=CommunityId(community_id), server_id=ServerId(server_id)
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    return [GroupResponse.from_entity(group) for group in groups]


async def _record(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    target_id: uuid.UUID,
) -> None:
    """Record a successful group mutation (FR-AUD-1), fire-after-commit."""

    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_GROUP,
            target_id=target_id,
        )
    )


def _parse_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise _unprocessable("invalid_player") from exc


def _unprocessable(reason: str) -> ProblemException:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)


def _conflict(reason: str) -> ProblemException:
    return problem(status.HTTP_409_CONFLICT, reason)


def _not_found() -> ProblemException:
    # Keep the no-existence-signal posture (Section 6.4): a group/server outside
    # this community 404s the same as a wholly unknown one.
    return problem(status.HTTP_404_NOT_FOUND, "not_found")
