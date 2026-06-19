"""HTTP edge for the global mod library and server assignment (issues #1261, #1262).

Mods are global (not community-scoped). The authenticated library routes live
under ``/mods``. Upload requires ``server:update`` in at least one community;
delete requires the caller to be the uploader or a platform admin; list and
download require only authentication.

Assignment routes (issue #1262) live under
``/communities/{community_id}/servers/{server_id}/mods`` and are permission-gated
per server (``server:update`` for assign/unassign/toggle, ``server:read`` for
list). Every assignment mutation is at-rest gated (409 ``server_unsettled`` while
the server is running) and physically (un)deploys the jar into the working set.

Modrinth import and the client modpack are later sub-issues of epic #1258 and are
not served here.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, UploadFile, status
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
    get_assign_mods,
    get_audit_recorder,
    get_current_user,
    get_delete_mod,
    get_download_mod,
    get_list_mods,
    get_list_server_mods,
    get_set_mod_enabled,
    get_unassign_mod,
    get_upload_mod,
    require_permission,
    require_server_update_in_any_community,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.application.mod_validation import ModValidation
from mc_server_dashboard_api.servers.application.mods import (
    MAX_MOD_BYTES,
    DeleteMod,
    DownloadMod,
    ListMods,
    UploadMod,
)
from mc_server_dashboard_api.servers.application.server_mods import (
    AssignMods,
    ListServerMods,
    ServerModSet,
    SetModEnabled,
    UnassignMod,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidModJarError,
    ModAssignmentNotFoundError,
    ModInUseError,
    ModNotFoundError,
    PermissionDeniedError,
    ServerBusyError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId, ModSide
from mc_server_dashboard_api.servers.domain.server_mod import ServerModAssignment
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

router = APIRouter()
assignment_router = APIRouter()

# Chunked read buffer for the upload body (same pattern as resource_packs.py).
_UPLOAD_CHUNK_BYTES = 1024 * 1024

_MOD_MEDIA_TYPE = "application/java-archive"


class ModResponse(BaseModel):
    """One mod's library metadata."""

    id: uuid.UUID
    filename: str
    display_name: str
    description: str | None
    loader_type: str
    mod_identifier: str
    provides: list[str]
    version_number: str
    mc_versions: list[str]
    side: str
    dependencies: list[dict[str, object]]
    sha256_hash: str
    sha512_hash: str | None
    size_bytes: int
    source: str
    uploaded_by: uuid.UUID
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @classmethod
    def from_mod(cls, mod: Mod) -> "ModResponse":
        return cls(
            id=mod.id.value,
            filename=mod.filename,
            display_name=mod.display_name,
            description=mod.description,
            loader_type=mod.loader_type,
            mod_identifier=mod.mod_identifier,
            provides=mod.provides,
            version_number=mod.version_number,
            mc_versions=mod.mc_versions,
            side=mod.side,
            dependencies=mod.dependencies,
            sha256_hash=mod.sha256_hash,
            sha512_hash=mod.sha512_hash,
            size_bytes=mod.size_bytes,
            source=mod.source,
            uploaded_by=mod.uploaded_by,
            created_at=mod.created_at,
            updated_at=mod.updated_at,
        )


class ModListResponse(BaseModel):
    mods: list[ModResponse]


@router.post("/mods", status_code=status.HTTP_201_CREATED)
async def upload_mod(
    file: UploadFile,
    display_name: Annotated[str, Form()],
    user: Annotated[User, Depends(require_server_update_in_any_community)],
    use_case: Annotated[UploadMod, Depends(get_upload_mod)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    side: Annotated[ModSide | None, Form()] = None,
) -> ModResponse:
    """Upload a mod jar (server:update in any community, issue #1261).

    Parses the manifest on ingest and dedups on SHA-256: an identical upload
    resolves to the existing library entry (returned 201, no duplicate stored).
    """

    filename = file.filename or "upload.jar"
    if not filename.lower().endswith(".jar"):
        raise _unprocessable("filename_not_jar")

    content = await _read_capped_upload(file)

    try:
        mod = await use_case(
            filename=filename,
            display_name=display_name,
            content=content,
            uploaded_by=user.id.value,
            side=side,
        )
    except InvalidModJarError as exc:
        raise _unprocessable("invalid_mod_jar") from exc
    except FileTooLargeError as exc:
        raise _too_large() from exc

    await recorder.record(
        AuditEvent(
            operation=ops.MOD_UPLOAD,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_MOD,
            target_id=mod.id.value,
        )
    )
    return ModResponse.from_mod(mod)


@router.get("/mods")
async def list_mods(
    _user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[ListMods, Depends(get_list_mods)],
    loader: Annotated[str | None, Query()] = None,
    mc: Annotated[str | None, Query()] = None,
    side: Annotated[str | None, Query()] = None,
) -> ModListResponse:
    """List library mods, optionally filtered (authenticated, issue #1261)."""

    mods = await use_case(loader_type=loader, mc_version=mc, side=side)
    return ModListResponse(mods=[ModResponse.from_mod(m) for m in mods])


@router.delete("/mods/{mod_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mod(
    mod_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[DeleteMod, Depends(get_delete_mod)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Delete a mod from the library (uploader or platform admin, issue #1261)."""

    try:
        await use_case(
            mod_id=ModId(mod_id),
            caller_id=user.id.value,
            is_platform_admin=user.is_platform_admin,
        )
    except ModNotFoundError as exc:
        raise _not_found() from exc
    except PermissionDeniedError as exc:
        raise _forbidden() from exc
    except ModInUseError as exc:
        raise _conflict("mod_in_use") from exc

    await recorder.record(
        AuditEvent(
            operation=ops.MOD_DELETE,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_MOD,
            target_id=mod_id,
        )
    )


@router.get("/mods/{mod_id}/download")
async def download_mod(
    mod_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    use_case: Annotated[DownloadMod, Depends(get_download_mod)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> StreamingResponse:
    """Download a mod jar (authenticated, issue #1261)."""

    try:
        stream, mod = await use_case(mod_id=ModId(mod_id))
    except ModNotFoundError as exc:
        raise _not_found() from exc

    await recorder.record(
        AuditEvent(
            operation=ops.MOD_DOWNLOAD,
            outcome=Outcome.SUCCESS,
            actor_id=user.id.value,
            target_type=ops.TARGET_MOD,
            target_id=mod_id,
        )
    )
    return StreamingResponse(
        stream,
        media_type=_MOD_MEDIA_TYPE,
        headers={"Content-Disposition": _content_disposition(mod.filename)},
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
        if total > MAX_MOD_BYTES:
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
# Assignment routes (issue #1262)
# ---------------------------------------------------------------------------

_ASSIGNMENT_BASE = "/communities/{community_id}/servers/{server_id}/mods"
_SERVER_RESOURCE_TYPE = "server"


class AssignModsRequest(BaseModel):
    """Multi-select assign: the library mod ids to add to the server's mod set."""

    mod_ids: list[uuid.UUID]


class ServerModResponse(BaseModel):
    """One entry of a server's mod set: the assignment plus its library mod."""

    mod: ModResponse
    enabled: bool
    assigned_by: uuid.UUID
    assigned_at: UtcDatetime

    @classmethod
    def from_assignment(
        cls, assignment: ServerModAssignment, mod: Mod
    ) -> "ServerModResponse":
        return cls(
            mod=ModResponse.from_mod(mod),
            enabled=assignment.enabled,
            assigned_by=assignment.assigned_by,
            assigned_at=assignment.created_at,
        )


class MissingDependencyResponse(BaseModel):
    """A required dependency that nothing in the mod set provides."""

    mod_id: str
    depends_on: str
    version_range: str


class ConflictResponse(BaseModel):
    """An assigned mod that conflicts with another present mod."""

    mod_id: str
    conflicts_with: str


class LoaderMismatchResponse(BaseModel):
    """An assigned mod whose loader the server cannot run."""

    mod_id: str
    mod_loader: str
    server_loader: str


class McMismatchResponse(BaseModel):
    """An assigned mod that does not list the server's MC version."""

    mod_id: str
    mod_mc_versions: list[str]
    server_mc_version: str


class ModValidationResponse(BaseModel):
    """The phase-B validation checklist for a server's mod set (issue #1263).

    Display-only: empty lists mean the set is fully valid. ``conflicts`` is empty
    for jars uploaded today (the manifest parser does not yet capture break
    entries); the field is present so it populates once breaks are parsed.
    """

    missing_deps: list[MissingDependencyResponse]
    conflicts: list[ConflictResponse]
    loader_mismatch: list[LoaderMismatchResponse]
    mc_mismatch: list[McMismatchResponse]

    @classmethod
    def from_validation(cls, validation: ModValidation) -> "ModValidationResponse":
        return cls(
            missing_deps=[
                MissingDependencyResponse(
                    mod_id=f.mod_id,
                    depends_on=f.depends_on,
                    version_range=f.version_range,
                )
                for f in validation.missing_deps
            ],
            conflicts=[
                ConflictResponse(mod_id=f.mod_id, conflicts_with=f.conflicts_with)
                for f in validation.conflicts
            ],
            loader_mismatch=[
                LoaderMismatchResponse(
                    mod_id=f.mod_id,
                    mod_loader=f.mod_loader,
                    server_loader=f.server_loader,
                )
                for f in validation.loader_mismatch
            ],
            mc_mismatch=[
                McMismatchResponse(
                    mod_id=f.mod_id,
                    mod_mc_versions=f.mod_mc_versions,
                    server_mc_version=f.server_mc_version,
                )
                for f in validation.mc_mismatch
            ],
        )


class ServerModListResponse(BaseModel):
    mods: list[ServerModResponse]
    validation: ModValidationResponse

    @classmethod
    def from_mod_set(cls, mod_set: ServerModSet) -> "ServerModListResponse":
        return cls(
            mods=[ServerModResponse.from_assignment(a, m) for a, m in mod_set.entries],
            validation=ModValidationResponse.from_validation(mod_set.validation),
        )


def _server_update_guard() -> object:
    return require_permission(
        Permission("server:update"),
        resource_type=_SERVER_RESOURCE_TYPE,
        resource_id_param="server_id",
    )


def _server_read_guard() -> object:
    return require_permission(
        Permission("server:read"),
        resource_type=_SERVER_RESOURCE_TYPE,
        resource_id_param="server_id",
    )


@assignment_router.post(_ASSIGNMENT_BASE, status_code=status.HTTP_201_CREATED)
async def assign_mods(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: AssignModsRequest,
    auth_user: Annotated[AuthUser, Depends(_server_update_guard())],
    use_case: Annotated[AssignMods, Depends(get_assign_mods)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    list_use_case: Annotated[ListServerMods, Depends(get_list_server_mods)],
) -> ServerModListResponse:
    """Assign one or more mods to a server (server:update, issue #1262)."""

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            mod_ids=[ModId(m) for m in body.mod_ids],
            assigned_by=auth_user.user_id.value,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ModNotFoundError as exc:
        raise _not_found() from exc
    except ServerFilesUnsettledError as exc:
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        raise _conflict("server_busy") from exc

    await recorder.record(
        AuditEvent(
            operation=ops.MOD_ASSIGN,
            outcome=Outcome.SUCCESS,
            actor_id=auth_user.user_id.value,
            target_type=ops.TARGET_SERVER,
            target_id=server_id,
        )
    )

    mod_set = await list_use_case(
        community_id=CommunityId(community_id),
        server_id=ServerId(server_id),
    )
    return ServerModListResponse.from_mod_set(mod_set)


@assignment_router.delete(
    _ASSIGNMENT_BASE + "/{mod_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unassign_mod(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    mod_id: uuid.UUID,
    auth_user: Annotated[AuthUser, Depends(_server_update_guard())],
    use_case: Annotated[UnassignMod, Depends(get_unassign_mod)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Unassign a mod from a server (server:update, issue #1262)."""

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            mod_id=ModId(mod_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ModAssignmentNotFoundError as exc:
        raise _not_found() from exc
    except ModNotFoundError as exc:
        raise _not_found() from exc
    except ServerFilesUnsettledError as exc:
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        raise _conflict("server_busy") from exc

    await recorder.record(
        AuditEvent(
            operation=ops.MOD_UNASSIGN,
            outcome=Outcome.SUCCESS,
            actor_id=auth_user.user_id.value,
            target_type=ops.TARGET_SERVER,
            target_id=server_id,
        )
    )


@assignment_router.post(
    _ASSIGNMENT_BASE + "/{mod_id}/enable",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def enable_mod(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    mod_id: uuid.UUID,
    auth_user: Annotated[AuthUser, Depends(_server_update_guard())],
    use_case: Annotated[SetModEnabled, Depends(get_set_mod_enabled)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Enable an assigned mod, redeploying its jar (server:update, issue #1262)."""

    await _toggle_status(
        community_id=community_id,
        server_id=server_id,
        mod_id=mod_id,
        enabled=True,
        auth_user=auth_user,
        use_case=use_case,
        recorder=recorder,
    )


@assignment_router.post(
    _ASSIGNMENT_BASE + "/{mod_id}/disable",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def disable_mod(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    mod_id: uuid.UUID,
    auth_user: Annotated[AuthUser, Depends(_server_update_guard())],
    use_case: Annotated[SetModEnabled, Depends(get_set_mod_enabled)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Disable an assigned mod, renaming its jar (server:update, issue #1262)."""

    await _toggle_status(
        community_id=community_id,
        server_id=server_id,
        mod_id=mod_id,
        enabled=False,
        auth_user=auth_user,
        use_case=use_case,
        recorder=recorder,
    )


async def _toggle_status(
    *,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    mod_id: uuid.UUID,
    enabled: bool,
    auth_user: AuthUser,
    use_case: SetModEnabled,
    recorder: AuditRecorder,
) -> None:
    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            mod_id=ModId(mod_id),
            enabled=enabled,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ModAssignmentNotFoundError as exc:
        raise _not_found() from exc
    except ModNotFoundError as exc:
        raise _not_found() from exc
    except ServerFilesUnsettledError as exc:
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        raise _conflict("server_busy") from exc

    await recorder.record(
        AuditEvent(
            operation=ops.MOD_ENABLE if enabled else ops.MOD_DISABLE,
            outcome=Outcome.SUCCESS,
            actor_id=auth_user.user_id.value,
            target_type=ops.TARGET_SERVER,
            target_id=server_id,
        )
    )


@assignment_router.get(_ASSIGNMENT_BASE)
async def list_server_mods(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _auth_user: Annotated[AuthUser, Depends(_server_read_guard())],
    use_case: Annotated[ListServerMods, Depends(get_list_server_mods)],
) -> ServerModListResponse:
    """List a server's mod set (server:read, issue #1262)."""

    try:
        mod_set = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc

    return ServerModListResponse.from_mod_set(mod_set)
