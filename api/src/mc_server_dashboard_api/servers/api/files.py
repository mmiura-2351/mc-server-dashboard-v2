"""HTTP edge for server file management (Section 6.10), with state branching.

Routes live under ``/communities/{community_id}/servers/{server_id}/files`` and
are *per-resource* gated (``resource_type='server'``,
``resource_id_param='server_id'``) like the server routes: a grant on one server
opens exactly that server's files (FR-AUTHZ-2). The catalog codes are ``file:read``
(browse + read), ``file:edit`` (write), ``file:history`` (versions), and
``file:rollback``.

File content for the JSON read/write routes is carried base64-encoded so they are
bytes-faithful (the proto fields are ``bytes``; no text/encoding mangling). Bulk
transfer takes a different shape (issue #259): ``/files/upload`` is a multipart
upload (``file:edit``) and ``/files/download`` streams a file's bytes or a
directory as a zip (``file:read``); both are at-rest only (running -> 409
``server_unsettled``) and are audited. The router is thin: it resolves use cases
via DI, runs them, and maps the servers file errors to HTTP codes (404 keeps the
no-existence-signal posture; a traversal-unsafe path is 422; an oversized edit /
upload is 413; a transitional server is 409; a disconnected worker is 503).

Running-server file failures carry a refined reason (issue #548): the Worker
emits one umbrella ``FILE_ACCESS_DENIED`` for several distinct conditions, so the
read/list/write routes surface an honest 422 ``reason`` instead of collapsing
every denial into ``invalid_path``. The file-API problem-reason catalog is:

- ``invalid_path`` (422) — a genuine path-syntax rejection (absolute, ``..``, or
  an unrefined denial / an older Worker). This is also the at-rest reason.
- ``is_a_directory`` (422) — a read or write whose path is a directory.
- ``not_a_directory`` (422) — a directory listing whose path is a regular file.
- ``symlink_refused`` (422) — the Worker refused to follow a path-component
  symlink (the FR-FILE-4 escape-vector defence).
- ``file_too_large`` (413) — a read result or an edit payload past the
  control-plane file cap (the edge ``MAX_EDIT_BYTES`` cap shares this reason).

A write (``PUT /files``) edits a file branching on server state (Section 6.9) and
**creates** the target when it does not exist yet — at rest or running alike
(create-through to the live working set). ``422 invalid_path`` means the path is
genuinely malformed (absolute, or contains ``..``); it never means "this file
does not exist yet", so creating a new file on a running server with a valid
relative path succeeds (204) rather than 422.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_delete_file,
    get_download_file,
    get_list_dir,
    get_list_file_versions,
    get_make_dir,
    get_read_file,
    get_rename_file,
    get_rollback_file,
    get_search_files,
    get_upload_file,
    get_write_file,
    require_permission,
)
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.servers.application.files import (
    MAX_UPLOAD_BYTES,
    DeleteFile,
    DownloadFile,
    ListDir,
    ListFileVersions,
    MakeDir,
    ReadFile,
    RenameFile,
    RollbackFile,
    SearchFiles,
    WriteFile,
)
from mc_server_dashboard_api.servers.application.files import (
    UploadFile as UploadFileUseCase,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    ContentDirProtectedError,
    FileAlreadyExistsError,
    FileTooLargeError,
    InvalidFilePathError,
    ServerBusyError,
    ServerFileNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileEntry
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"

# How much of the multipart body to pull per chunk while counting it against the
# upload cap (the bounded-read loop in ``_read_capped_upload``).
_UPLOAD_CHUNK_BYTES = 1024 * 1024


class FileContentResponse(BaseModel):
    """A file's bytes, base64-encoded for JSON transport."""

    path: str
    content_base64: str


class WriteFileRequest(BaseModel):
    content_base64: str = Field(default="")


class DirEntryResponse(BaseModel):
    name: str
    is_dir: bool
    size: int

    @classmethod
    def from_entry(cls, entry: FileEntry) -> "DirEntryResponse":
        return cls(name=entry.name, is_dir=entry.is_dir, size=entry.size)


class DirListingResponse(BaseModel):
    path: str
    entries: list[DirEntryResponse]
    truncated: bool = False


class FileVersionsResponse(BaseModel):
    path: str
    versions: list[str]


class RollbackRequest(BaseModel):
    version_id: str = Field(min_length=1)


class RenameRequest(BaseModel):
    """Source and destination rel-paths for a rename (issue #259)."""

    # Aliased to the issue's ``from`` / ``to`` field names (``from`` is a Python
    # keyword, so the model attributes are ``from_`` / ``to``).
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)


class SearchRequest(BaseModel):
    """A name/content search query with a result cap (issue #259)."""

    query: str
    by: str = Field(default="name")
    max_results: int = Field(default=100, ge=1)


class SearchResponse(BaseModel):
    paths: list[str]
    truncated: bool


@router.get("/communities/{community_id}/servers/{server_id}/files")
async def read_or_list_files(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("file:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    read_use_case: Annotated[ReadFile, Depends(get_read_file)],
    list_use_case: Annotated[ListDir, Depends(get_list_dir)],
    path: Annotated[str, Query()] = ".",
    list_dir: Annotated[bool, Query(alias="list")] = False,
) -> FileContentResponse | DirListingResponse:
    """Read a file (default) or browse a directory (``?list=true``).

    Both reads and browsing branch on server state (Section 6.9): a running
    server is served from the Worker's live working set, a server at rest from
    the authoritative copy.
    """

    if list_dir:
        try:
            listing = await list_use_case(
                community_id=CommunityId(community_id),
                server_id=ServerId(server_id),
                rel_path=path,
            )
        except ServerNotFoundError as exc:
            raise _not_found() from exc
        except ServerFileNotFoundError as exc:
            raise _not_found() from exc
        except InvalidFilePathError as exc:
            # exc.reason refines a running-server file denial (issue #548): a
            # non-path condition (not_a_directory / symlink_refused) surfaces
            # honestly instead of a blanket invalid_path.
            raise _unprocessable(exc.reason) from exc
        except ServerFilesUnsettledError as exc:
            raise _conflict("server_unsettled") from exc
        except WorkerUnavailableError as exc:
            raise _service_unavailable("worker_unavailable") from exc
        except CommandDispatchError as exc:
            raise _conflict("command_failed") from exc
        return DirListingResponse(
            path=path,
            entries=[DirEntryResponse.from_entry(e) for e in listing.entries],
            truncated=listing.truncated,
        )

    # This base64 JSON read stays whole-bytes by design (not streamed like the
    # file download, issue #265): the bytes ARE the response payload (base64 in
    # the JSON body), so there is nothing to stream into — it is an interactive
    # small-file read, not a bulk download. A large file is downloaded via
    # /files/download (the streamed branch).
    try:
        content = await read_use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        # exc.reason refines a running-server file denial (issue #548): a
        # non-path condition (is_a_directory / symlink_refused) surfaces honestly
        # instead of a blanket invalid_path.
        raise _unprocessable(exc.reason) from exc
    except FileTooLargeError as exc:
        # A running-server read of a file past the control-plane cap (issue #548):
        # the Worker reports payload_too_large, mapped to 413 like an edit cap.
        raise _too_large() from exc
    except ServerFilesUnsettledError as exc:
        raise _conflict("server_unsettled") from exc
    except WorkerUnavailableError as exc:
        raise _service_unavailable("worker_unavailable") from exc
    except CommandDispatchError as exc:
        raise _conflict("command_failed") from exc
    return FileContentResponse(
        path=path, content_base64=base64.b64encode(content).decode("ascii")
    )


@router.put(
    "/communities/{community_id}/servers/{server_id}/files",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def write_file(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: WriteFileRequest,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("file:edit"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[WriteFile, Depends(get_write_file)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    path: Annotated[str, Query()] = ".",
) -> None:
    """Edit a file, branching on server state (Section 6.9).

    A successful write is audited (``file:write``); a write refused because the
    server is unsettled is recorded DENIED, matching the upload posture (issue
    #263).
    """

    content = _decode(body.content_base64)
    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
            content=content,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        # exc.reason refines a running-server file denial (issue #548): a non-path
        # condition (is_a_directory / symlink_refused) surfaces honestly instead
        # of a blanket invalid_path. An at-rest write keeps the default invalid_path.
        raise _unprocessable(exc.reason) from exc
    except FileTooLargeError as exc:
        # The edge cap (MAX_EDIT_BYTES) and the Worker's payload_too_large reason
        # (issue #548) both surface here as 413.
        raise _too_large() from exc
    except ContentDirProtectedError as exc:
        raise _conflict("content_dir_protected") from exc
    except ServerFilesUnsettledError as exc:
        await _record_file_failure(
            recorder, ops.FILE_WRITE, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_file_failure(
            recorder, ops.FILE_WRITE, authorized, community_id, server_id
        )
        raise _conflict("server_busy") from exc
    except WorkerUnavailableError as exc:
        raise _service_unavailable("worker_unavailable") from exc
    except CommandDispatchError as exc:
        raise _conflict("command_failed") from exc
    await _record_file(recorder, ops.FILE_WRITE, authorized, community_id, server_id)


@router.get("/communities/{community_id}/servers/{server_id}/files/history")
async def list_file_history(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("file:history"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[ListFileVersions, Depends(get_list_file_versions)],
    path: Annotated[str, Query()],
) -> FileVersionsResponse:
    """List retained prior versions of a file (file:history)."""

    try:
        versions = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    return FileVersionsResponse(path=path, versions=versions)


@router.post(
    "/communities/{community_id}/servers/{server_id}/files/rollback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def rollback_file(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: RollbackRequest,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("file:rollback"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[RollbackFile, Depends(get_rollback_file)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    path: Annotated[str, Query()],
) -> None:
    """Roll a file back to a retained version (file:rollback).

    Requires the server at rest (Section 6.9): rollback republishes the
    authoritative copy, so it is 409 while running. A successful rollback is
    audited (``file:rollback``); a rollback refused because the server is not
    stopped is recorded DENIED (issue #263).
    """

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
            version_id=body.version_id,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except ServerNotStoppedError as exc:
        await _record_file_failure(
            recorder, ops.FILE_ROLLBACK, authorized, community_id, server_id
        )
        raise _conflict("server_not_stopped") from exc
    except ServerBusyError as exc:
        await _record_file_failure(
            recorder, ops.FILE_ROLLBACK, authorized, community_id, server_id
        )
        raise _conflict("server_busy") from exc
    await _record_file(recorder, ops.FILE_ROLLBACK, authorized, community_id, server_id)


@router.post(
    "/communities/{community_id}/servers/{server_id}/files/upload",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def upload_file(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    file: UploadFile,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("file:edit"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[UploadFileUseCase, Depends(get_upload_file)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    path: Annotated[str, Query()] = ".",
    extract: Annotated[bool, Query()] = False,
) -> None:
    """Upload a multipart file into ``path`` at rest (file:edit, FR-FILE-*).

    At rest only (Section 6.9): a running server is 409 ``server_unsettled``,
    reusing the unsettled posture other bulk at-rest ops take. With
    ``extract=true`` a zip / tar.gz is expanded under ``path`` with per-entry
    traversal validation (zip-slip defence) and a total-extracted-size cap.
    """

    filename = file.filename or ""
    content = await _read_capped_upload(file)
    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            dir_path=path,
            filename=filename,
            content=content,
            extract=extract,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except FileTooLargeError as exc:
        raise _too_large() from exc
    except ContentDirProtectedError as exc:
        raise _conflict("content_dir_protected") from exc
    except ServerFilesUnsettledError as exc:
        await _record_file_failure(
            recorder, ops.FILE_UPLOAD, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_file_failure(
            recorder, ops.FILE_UPLOAD, authorized, community_id, server_id
        )
        raise _conflict("server_busy") from exc
    await _record_file(recorder, ops.FILE_UPLOAD, authorized, community_id, server_id)


@router.get("/communities/{community_id}/servers/{server_id}/files/download")
async def download_file(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("file:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[DownloadFile, Depends(get_download_file)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    path: Annotated[str, Query()] = ".",
) -> Response:
    """Download a file (bytes) or a directory (streamed zip) at rest (file:read).

    At rest only (Section 6.9): a running server is 409 ``server_unsettled``. A
    directory streams as a zip built incrementally over the Storage read stream
    (bounded memory); a file streams its bytes with an attachment disposition.
    """

    try:
        is_dir = await use_case.is_dir(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
        )
        if is_dir:
            stream = await use_case.dir_zip(
                community_id=CommunityId(community_id),
                server_id=ServerId(server_id),
                rel_path=path,
            )
            name = posixpath.basename(path.rstrip("/")) or "root"
            response: Response = StreamingResponse(
                stream,
                media_type="application/zip",
                headers={"Content-Disposition": _content_disposition(f"{name}.zip")},
            )
        else:
            # Stream the file's bytes (issue #265) so a large single-file download
            # never buffers the whole file in RAM. The size is resolved from the
            # cheap parent listing for a Content-Length header when known; absent
            # (e.g. the path has no listable parent), the response falls back to
            # chunked transfer.
            size = await use_case.file_size(
                community_id=CommunityId(community_id),
                server_id=ServerId(server_id),
                rel_path=path,
            )
            file_stream = await use_case.file_stream(
                community_id=CommunityId(community_id),
                server_id=ServerId(server_id),
                rel_path=path,
            )
            name = posixpath.basename(path) or "download"
            headers = {"Content-Disposition": _content_disposition(name)}
            if size is not None:
                headers["Content-Length"] = str(size)
            response = StreamingResponse(
                file_stream,
                media_type="application/octet-stream",
                headers=headers,
            )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except ServerFilesUnsettledError as exc:
        await _record_file_failure(
            recorder, ops.FILE_DOWNLOAD, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    await _record_file(recorder, ops.FILE_DOWNLOAD, authorized, community_id, server_id)
    return response


@router.post(
    "/communities/{community_id}/servers/{server_id}/files/rename",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def rename_file(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: RenameRequest,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("file:edit"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[RenameFile, Depends(get_rename_file)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Rename/move a file at rest (file:edit, FR-FILE-*).

    At rest only (Section 6.9): a running server is 409 ``server_unsettled``. Both
    paths are traversal-validated (422 on a bad path); a missing source is 404 and
    an existing destination is 409 ``destination_exists`` (rename never clobbers).
    """

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            from_path=body.from_,
            to_path=body.to,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except FileAlreadyExistsError as exc:
        raise _conflict("destination_exists") from exc
    except ContentDirProtectedError as exc:
        raise _conflict("content_dir_protected") from exc
    except ServerFilesUnsettledError as exc:
        await _record_file_failure(
            recorder, ops.FILE_RENAME, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_file_failure(
            recorder, ops.FILE_RENAME, authorized, community_id, server_id
        )
        raise _conflict("server_busy") from exc
    await _record_file(recorder, ops.FILE_RENAME, authorized, community_id, server_id)


@router.delete(
    "/communities/{community_id}/servers/{server_id}/files",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_file(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("file:edit"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[DeleteFile, Depends(get_delete_file)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    path: Annotated[str, Query()],
) -> None:
    """Delete a file or directory (recursive) at rest (file:edit, FR-FILE-*).

    At rest only (Section 6.9): a running server is 409 ``server_unsettled``. The
    path is resolved to a file or directory; a missing path is 404 and a
    traversal-unsafe one is 422. A file delete retains the prior content (rollback
    can resurrect it); a directory delete does not (backups cover whole subtrees).
    """

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except ContentDirProtectedError as exc:
        raise _conflict("content_dir_protected") from exc
    except ServerFilesUnsettledError as exc:
        await _record_file_failure(
            recorder, ops.FILE_DELETE, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_file_failure(
            recorder, ops.FILE_DELETE, authorized, community_id, server_id
        )
        raise _conflict("server_busy") from exc
    await _record_file(recorder, ops.FILE_DELETE, authorized, community_id, server_id)


@router.post(
    "/communities/{community_id}/servers/{server_id}/files/directories",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def make_directory(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("file:edit"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[MakeDir, Depends(get_make_dir)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    path: Annotated[str, Query()],
) -> None:
    """Create an (empty) directory at rest (file:edit, FR-FILE-*).

    At rest only (Section 6.9): a running server is 409 ``server_unsettled``. The
    path is traversal-validated (422). Backend-dependent: object storage cannot
    represent an empty directory (the seam is a no-op there) — the directory
    becomes observable once a file is written under it.
    """

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except ServerFilesUnsettledError as exc:
        await _record_file_failure(
            recorder, ops.FILE_MKDIR, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        await _record_file_failure(
            recorder, ops.FILE_MKDIR, authorized, community_id, server_id
        )
        raise _conflict("server_busy") from exc
    await _record_file(recorder, ops.FILE_MKDIR, authorized, community_id, server_id)


@router.post("/communities/{community_id}/servers/{server_id}/files/search")
async def search_files(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: SearchRequest,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("file:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[SearchFiles, Depends(get_search_files)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> SearchResponse:
    """Search the authoritative copy by name or content at rest (file:read).

    At rest only (Section 6.9): a running server is 409 ``server_unsettled``,
    matching the other three mutations' posture (search reads the authoritative
    Storage copy, which is only well-defined at rest). ``by`` must be ``name`` or
    ``content`` (else 422). Results are bounded; ``truncated`` flags a clipped
    result.
    """

    try:
        result = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            query=body.query,
            by=body.by,
            max_results=body.max_results,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except ServerFilesUnsettledError as exc:
        await _record_file_failure(
            recorder, ops.FILE_SEARCH, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc
    await _record_file(recorder, ops.FILE_SEARCH, authorized, community_id, server_id)
    return SearchResponse(paths=result.paths, truncated=result.truncated)


async def _read_capped_upload(file: UploadFile) -> bytes:
    """Pull the multipart body in chunks, aborting with 413 past the upload cap.

    Starlette spools the multipart part to a temp file as it parses the request,
    but ``file.read()`` with no argument then pulls the whole part into memory at
    once. Reading in bounded chunks and checking the running count after each lets
    an over-cap upload be refused as soon as the count crosses MAX_UPLOAD_BYTES,
    rather than materializing a body far larger than the cap first (mirroring the
    streamed byte-counting in ``dataplane/api/transfers.py``). The use case
    re-checks the cap, so this is the edge's early-out, not the only guard.
    """

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _content_disposition(filename: str) -> str:
    """Build an attachment Content-Disposition header (RFC 6266 / RFC 5987).

    A filename can contain ``"`` (which would break out of the quoted-string and
    let a crafted name inject extra header parameters) or non-latin-1 characters
    (which raise ``UnicodeEncodeError`` when Starlette latin-1-encodes the header,
    500-ing a legitimate Unicode upload). So emit two parameters: an ASCII-only
    ``filename`` fallback (non-ASCII/quote/backslash/control chars replaced) for
    legacy clients, plus an RFC 5987 ``filename*`` carrying the UTF-8 percent-
    encoded original for modern clients, which prefer it.
    """

    ascii_fallback = "".join(
        c if (0x20 <= ord(c) < 0x7F and c not in '"\\') else "_" for c in filename
    )
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


async def _record_file(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
) -> None:
    """Record a successful file upload/download (FR-AUD-1).

    A file has no UUID id of its own, so the event targets the owning server
    (``target_type=file``); the path lives off the audit row's UUID columns.
    """

    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_FILE,
            target_id=server_id,
        )
    )


async def _record_file_failure(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
) -> None:
    """Record a refused file op (DENIED — server unsettled), targeting the server."""

    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.DENIED,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_FILE,
            target_id=server_id,
        )
    )


def _decode(content_base64: str) -> bytes:
    try:
        return base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _unprocessable("invalid_base64") from exc


def _unprocessable(reason: str) -> ProblemException:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)


def _too_large() -> ProblemException:
    return problem(status.HTTP_413_CONTENT_TOO_LARGE, "file_too_large")


def _service_unavailable(reason: str) -> ProblemException:
    return problem(status.HTTP_503_SERVICE_UNAVAILABLE, reason)


def _conflict(reason: str) -> ProblemException:
    return problem(status.HTTP_409_CONFLICT, reason)


def _not_found() -> ProblemException:
    # Keep the no-existence-signal posture (Section 6.4): a server/file outside
    # this community 404s the same as a wholly unknown one.
    return problem(status.HTTP_404_NOT_FOUND, "not_found")
