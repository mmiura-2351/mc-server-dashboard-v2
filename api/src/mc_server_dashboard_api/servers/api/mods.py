"""HTTP edge for the global mod library (issue #1261).

Mods are global (not community-scoped). The authenticated routes live under
``/mods``. Upload requires ``server:update`` in at least one community; delete
requires the caller to be the uploader or a platform admin; list and download
require only authentication.

Server assignment, Modrinth import, and the client modpack are later sub-issues
of epic #1258 and are not served here.
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
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_current_user,
    get_delete_mod,
    get_download_mod,
    get_list_mods,
    get_upload_mod,
    require_server_update_in_any_community,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.application.mods import (
    MAX_MOD_BYTES,
    DeleteMod,
    DownloadMod,
    ListMods,
    UploadMod,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidModJarError,
    ModNotFoundError,
    PermissionDeniedError,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId, ModSide

router = APIRouter()

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
