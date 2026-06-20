"""HTTP edge for the resource pack library (issues #1176, #1177).

Resource packs are global (not community-scoped). The authenticated routes live
under ``/resource-packs``; the unauthenticated public download endpoint lives
under ``/public/resource-packs/{id}/{filename}`` and is served by a separate
router so the auth middleware does not apply.

Upload requires ``server:update`` in at least one community. Delete requires
the caller to be the uploader or a platform admin. List and download require
only authentication. The public endpoint requires no authentication.

Assignment routes (issue #1177) live under
``/communities/{community_id}/servers/{server_id}/resource-pack`` and are
permission-gated per server (``server:update`` for assign/unassign,
``server:read`` for get).
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    Permission,
)
from mc_server_dashboard_api.dependencies import (
    get_assign_resource_pack,
    get_audit_recorder,
    get_current_user,
    get_delete_resource_pack,
    get_download_resource_pack,
    get_get_resource_pack_assignment,
    get_list_resource_packs,
    get_settings,
    get_unassign_resource_pack,
    get_upload_resource_pack,
    require_permission,
    require_server_update_in_any_community,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.application.resource_packs import (
    MAX_RESOURCE_PACK_BYTES,
    AssignResourcePack,
    DeleteResourcePack,
    DownloadResourcePack,
    GetResourcePackAssignment,
    ListResourcePacks,
    UnassignResourcePack,
    UploadResourcePack,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidResourcePackError,
    PermissionDeniedError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
    ServerBusyError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackId,
)

router = APIRouter()
public_router = APIRouter()
assignment_router = APIRouter()

# Chunked read buffer for the upload body (same pattern as backups.py).
_UPLOAD_CHUNK_BYTES = 1024 * 1024

_PACK_MEDIA_TYPE = "application/zip"


class ResourcePackResponse(BaseModel):
    """One resource pack's metadata."""

    id: uuid.UUID
    filename: str
    display_name: str
    description: str | None
    sha1_hash: str
    sha256_hash: str
    size_bytes: int
    download_url: str
    uploaded_by: uuid.UUID
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @classmethod
    def from_pack(cls, pack: ResourcePack, *, base_url: str) -> "ResourcePackResponse":
        download_url = (
            f"{base_url}/api/public/resource-packs/"
            f"{pack.id.value}/{quote(pack.filename, safe='')}"
        )
        return cls(
            id=pack.id.value,
            filename=pack.filename,
            display_name=pack.display_name,
            description=pack.description,
            sha1_hash=pack.sha1_hash,
            sha256_hash=pack.sha256_hash,
            size_bytes=pack.size_bytes,
            download_url=download_url,
            uploaded_by=pack.uploaded_by,
            created_at=pack.created_at,
            updated_at=pack.updated_at,
        )


class ResourcePackListResponse(BaseModel):
    resource_packs: list[ResourcePackResponse]


def _public_base_url(request: Request) -> str:
    """Resolve the externally-reachable base URL from settings.

    Falls back to an empty string when unset (the download_url will then be a
    relative path, which is acceptable in dev/test environments).
    """

    return get_settings(request).server.public_base_url or ""


@router.post(
    "/resource-packs",
    status_code=status.HTTP_201_CREATED,
)
async def upload_resource_pack(
    file: UploadFile,
    display_name: Annotated[str, Form()],
    user: Annotated[User, Depends(require_server_update_in_any_community)],
    use_case: Annotated[UploadResourcePack, Depends(get_upload_resource_pack)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    base_url: Annotated[str, Depends(_public_base_url)],
) -> ResourcePackResponse:
    """Upload a resource pack (server:update in any community, issue #1176)."""

    filename = file.filename or "upload.zip"
    if not filename.lower().endswith(".zip"):
        raise _unprocessable("filename_not_zip")

    content = await _read_capped_upload(file)

    try:
        pack = await use_case(
            filename=filename,
            display_name=display_name,
            content=content,
            uploaded_by=user.id.value,
        )
    except InvalidResourcePackError as exc:
        raise _unprocessable("invalid_resource_pack") from exc
    except FileTooLargeError as exc:
        raise _too_large() from exc

    await recorder.record(
        AuditEvent(
            operation=ops.RESOURCE_PACK_UPLOAD,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_RESOURCE_PACK,
            target_id=pack.id.value,
        )
    )
    return ResourcePackResponse.from_pack(pack, base_url=base_url)


@router.get("/resource-packs")
async def list_resource_packs(
    _user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ListResourcePacks, Depends(get_list_resource_packs)],
    base_url: Annotated[str, Depends(_public_base_url)],
) -> ResourcePackListResponse:
    """List all resource packs (authenticated, issue #1176)."""

    packs = await use_case()
    return ResourcePackListResponse(
        resource_packs=[
            ResourcePackResponse.from_pack(p, base_url=base_url) for p in packs
        ]
    )


@router.delete(
    "/resource-packs/{resource_pack_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_resource_pack(
    resource_pack_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[DeleteResourcePack, Depends(get_delete_resource_pack)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Delete a resource pack (uploader or platform admin, issue #1176)."""

    try:
        await use_case(
            resource_pack_id=ResourcePackId(resource_pack_id),
            caller_id=user.id.value,
            is_platform_admin=user.is_platform_admin,
        )
    except ResourcePackNotFoundError as exc:
        raise _not_found() from exc
    except PermissionDeniedError as exc:
        raise _forbidden() from exc
    except ResourcePackInUseError as exc:
        raise _conflict("resource_pack_in_use") from exc

    await recorder.record(
        AuditEvent(
            operation=ops.RESOURCE_PACK_DELETE,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_RESOURCE_PACK,
            target_id=resource_pack_id,
        )
    )


@router.get("/resource-packs/{resource_pack_id}/download")
async def download_resource_pack(
    resource_pack_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[DownloadResourcePack, Depends(get_download_resource_pack)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> StreamingResponse:
    """Download a resource pack (authenticated, issue #1176)."""

    try:
        stream, pack = await use_case(
            resource_pack_id=ResourcePackId(resource_pack_id),
        )
    except ResourcePackNotFoundError as exc:
        raise _not_found() from exc

    await recorder.record(
        AuditEvent(
            operation=ops.RESOURCE_PACK_DOWNLOAD,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_RESOURCE_PACK,
            target_id=resource_pack_id,
        )
    )
    return StreamingResponse(
        stream,
        media_type=_PACK_MEDIA_TYPE,
        headers={"Content-Disposition": _content_disposition(pack.filename)},
    )


@public_router.get("/public/resource-packs/{resource_pack_id}/{filename}")
async def public_download_resource_pack(
    resource_pack_id: uuid.UUID,
    filename: str,
    use_case: Annotated[DownloadResourcePack, Depends(get_download_resource_pack)],
) -> StreamingResponse:
    """Public download endpoint for Minecraft clients (no auth, issue #1176).

    Validates that ``filename`` matches the stored filename (404 otherwise).
    """

    try:
        stream, pack = await use_case(
            resource_pack_id=ResourcePackId(resource_pack_id),
        )
    except ResourcePackNotFoundError as exc:
        raise _not_found() from exc

    if pack.filename != filename:
        raise _not_found()

    return StreamingResponse(
        stream,
        media_type=_PACK_MEDIA_TYPE,
        headers={"Content-Disposition": _content_disposition(pack.filename)},
    )


async def _read_capped_upload(file: UploadFile) -> bytes:
    """Pull the multipart body in chunks, aborting with 413 past the cap."""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESOURCE_PACK_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _content_disposition(filename: str) -> str:
    """Build an attachment Content-Disposition header (RFC 6266 / RFC 5987)."""

    ascii_fallback = "".join(
        c if (0x20 <= ord(c) < 0x7F and c not in '"\\') else "_" for c in filename
    )
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def _not_found() -> ProblemException:
    return problem(status.HTTP_404_NOT_FOUND, "not_found")


def _forbidden() -> ProblemException:
    return problem(status.HTTP_403_FORBIDDEN, "forbidden")


def _too_large() -> ProblemException:
    return problem(status.HTTP_413_CONTENT_TOO_LARGE, "too_large")


def _unprocessable(reason: str) -> ProblemException:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)


def _conflict(reason: str) -> ProblemException:
    return problem(status.HTTP_409_CONFLICT, reason)


# ---------------------------------------------------------------------------
# Assignment routes (issue #1177)
# ---------------------------------------------------------------------------

_ASSIGNMENT_PATH = "/communities/{community_id}/servers/{server_id}/resource-pack"
_SERVER_RESOURCE_TYPE = "server"


class AssignResourcePackRequest(BaseModel):
    resource_pack_id: uuid.UUID
    require_resource_pack: bool = False
    resource_pack_prompt: str | None = None


class ResourcePackAssignmentResponse(BaseModel):
    resource_pack: ResourcePackResponse
    require_resource_pack: bool
    resource_pack_prompt: str | None
    assigned_by: uuid.UUID
    assigned_at: UtcDatetime


@assignment_router.post(_ASSIGNMENT_PATH)
async def assign_resource_pack(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: AssignResourcePackRequest,
    auth_user: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("server:update"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[AssignResourcePack, Depends(get_assign_resource_pack)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    base_url: Annotated[str, Depends(_public_base_url)],
) -> ResourcePackAssignmentResponse:
    """Assign a resource pack to a server (server:update, issue #1177)."""

    from mc_server_dashboard_api.servers.domain.value_objects import (
        CommunityId,
        ServerId,
    )

    try:
        assignment, pack = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            resource_pack_id=ResourcePackId(body.resource_pack_id),
            require_resource_pack=body.require_resource_pack,
            resource_pack_prompt=body.resource_pack_prompt,
            assigned_by=auth_user.user_id.value,
            public_base_url=base_url,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ResourcePackNotFoundError as exc:
        raise _not_found() from exc
    except ServerFilesUnsettledError as exc:
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        raise _conflict("server_busy") from exc

    await recorder.record(
        AuditEvent(
            operation=ops.RESOURCE_PACK_ASSIGN,
            outcome=Outcome.SUCCESS,
            actor_id=auth_user.user_id.value,
            target_type=ops.TARGET_SERVER,
            target_id=server_id,
        )
    )

    return ResourcePackAssignmentResponse(
        resource_pack=ResourcePackResponse.from_pack(pack, base_url=base_url),
        require_resource_pack=assignment.require_resource_pack,
        resource_pack_prompt=assignment.resource_pack_prompt,
        assigned_by=assignment.assigned_by,
        assigned_at=assignment.created_at,
    )


@assignment_router.delete(
    _ASSIGNMENT_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unassign_resource_pack(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    auth_user: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("server:update"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[UnassignResourcePack, Depends(get_unassign_resource_pack)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Unassign the resource pack from a server (server:update, issue #1177)."""

    from mc_server_dashboard_api.servers.domain.value_objects import (
        CommunityId,
        ServerId,
    )

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ResourcePackNotFoundError as exc:
        raise _not_found() from exc
    except ServerFilesUnsettledError as exc:
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        raise _conflict("server_busy") from exc

    await recorder.record(
        AuditEvent(
            operation=ops.RESOURCE_PACK_UNASSIGN,
            outcome=Outcome.SUCCESS,
            actor_id=auth_user.user_id.value,
            target_type=ops.TARGET_SERVER,
            target_id=server_id,
        )
    )


@assignment_router.get(_ASSIGNMENT_PATH)
async def get_resource_pack_assignment(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _auth_user: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("server:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[
        GetResourcePackAssignment, Depends(get_get_resource_pack_assignment)
    ],
    base_url: Annotated[str, Depends(_public_base_url)],
) -> ResourcePackAssignmentResponse:
    """Get the resource pack assignment for a server (server:read, issue #1177)."""

    from mc_server_dashboard_api.servers.domain.value_objects import (
        CommunityId,
        ServerId,
    )

    try:
        result = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc

    if result is None:
        raise _not_found()

    assignment, pack = result

    return ResourcePackAssignmentResponse(
        resource_pack=ResourcePackResponse.from_pack(pack, base_url=base_url),
        require_resource_pack=assignment.require_resource_pack,
        resource_pack_prompt=assignment.resource_pack_prompt,
        assigned_by=assignment.assigned_by,
        assigned_at=assignment.created_at,
    )
