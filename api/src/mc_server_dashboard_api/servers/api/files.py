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
- ``symlink_refused`` (422) — a path-component symlink was refused rather than
  followed (the FR-FILE-4 escape-vector defence). Both branches produce it: the
  Worker for a running server, and Storage for one at rest (issue #2432), so the
  same request gets the same reason in either state.
- ``invalid_version_id`` (422) — a malformed ``version_id`` (outside the
  ``VersionId`` charset) on the rollback / version-preview routes.
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
    Request,
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
    get_read_file_version,
    get_rename_file,
    get_rollback_file,
    get_search_files,
    get_token_service,
    get_upload_file,
    get_write_file,
    path_uuid,
    require_download_access,
    require_permission,
)
from mc_server_dashboard_api.http_content_disposition import content_disposition
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_head import head_response
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.identity.domain.token_service import TokenService
from mc_server_dashboard_api.identity.domain.value_objects import (
    UserId as IdentityUserId,
)
from mc_server_dashboard_api.servers.application.files import (
    MAX_UPLOAD_BYTES,
    DeleteFile,
    DownloadFile,
    ListDir,
    ListFileVersions,
    MakeDir,
    ReadFile,
    ReadFileVersion,
    RenameFile,
    RollbackFile,
    SearchFiles,
    WriteFile,
    file_download_grant_resource,
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
    InvalidVersionIdError,
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

# The ``?path=`` default shared by the download and its grant mint. The two must
# read the same value for an absent parameter, or a grant minted without a path
# would not redeem the URL it names.
_DEFAULT_DOWNLOAD_PATH = "."

# The download's two branches: a directory streams as a zip, a file as opaque
# bytes. The HEAD probe declares the same type as the body it stands for.
_ZIP_MEDIA_TYPE = "application/zip"
_FILE_MEDIA_TYPE = "application/octet-stream"

# Named once because two decorators register it: Starlette does not synthesize
# HEAD from a GET route, so the probe has to be declared (issue #2383). Both
# registrations point at the same endpoint function, so the gate, the headers and
# the error mapping cannot drift apart between the two methods.
_DOWNLOAD_PATH = "/communities/{community_id}/servers/{server_id}/files/download"


def _download_grant_resource(request: Request) -> str:
    """Bind a file download grant to the request's server and ``?path=``.

    The *same* callable runs at issuance and at redemption, so the two cannot
    build different strings for the same URL.
    """

    return file_download_grant_resource(
        path_uuid(request, "community_id"),
        path_uuid(request, "server_id"),
        request.query_params.get("path", _DEFAULT_DOWNLOAD_PATH),
    )


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
            # exc.reason refines a file denial (issue #548): a non-path condition
            # (not_a_directory / symlink_refused) surfaces honestly instead of a
            # blanket invalid_path. Running or at rest — Storage refuses a
            # path-component symlink with the same reason (issue #2432).
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
        # exc.reason refines a file denial (issue #548): a non-path condition
        # (is_a_directory / symlink_refused) surfaces honestly instead of a blanket
        # invalid_path. Running or at rest — Storage refuses a path-component
        # symlink with the same reason (issue #2432).
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
        # exc.reason refines a file denial (issue #548): a non-path condition
        # (is_a_directory / symlink_refused) surfaces honestly instead of a blanket
        # invalid_path. An at-rest write keeps invalid_path except under a symlink
        # parent, which Storage refuses with symlink_refused (issue #2432).
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


@router.get("/communities/{community_id}/servers/{server_id}/files/version")
async def read_file_version(
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
    use_case: Annotated[ReadFileVersion, Depends(get_read_file_version)],
    path: Annotated[str, Query()],
    version_id: Annotated[str, Query()],
) -> FileContentResponse:
    """Read a specific retained version's content (file:read, preview).

    Reached from the history drawer (``file:history``-gated to list versions),
    this previews a prior version's bytes read-only before a rollback. It returns
    file content, so it is gated by ``file:read`` like the current-file read
    route — ``file:history`` enumerates versions but does not grant content
    access. Authoritative-only like ``/history``; an unknown path/version is 404,
    a traversal-unsafe path is 422 ``invalid_path``, and a malformed version id is
    422 ``invalid_version_id``. The bytes are base64-encoded for JSON transport,
    matching the read route.
    """

    try:
        content = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
            version_id=version_id,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        raise _unprocessable("invalid_path") from exc
    except InvalidVersionIdError as exc:
        raise _unprocessable("invalid_version_id") from exc
    return FileContentResponse(
        path=path, content_base64=base64.b64encode(content).decode("ascii")
    )


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
    except InvalidVersionIdError as exc:
        raise _unprocessable("invalid_version_id") from exc
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


@router.get(_DOWNLOAD_PATH)
@router.head(_DOWNLOAD_PATH)
async def download_file(
    request: Request,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_download_access(Permission("file:read"), _download_grant_resource)
        ),
    ],
    use_case: Annotated[DownloadFile, Depends(get_download_file)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    path: Annotated[str, Query()] = _DEFAULT_DOWNLOAD_PATH,
) -> Response:
    """Download a file (bytes) or a directory (streamed zip) at rest (file:read).

    At rest only (Section 6.9): a running server is 409 ``server_unsettled``. A
    directory streams as a zip built incrementally over the Storage read stream
    (bounded memory); a file streams its bytes with an attachment disposition.

    The directory zip is built incrementally, so it carries no ``Content-Length``
    and a browser cannot cap it up front; a multi-GB ``world`` is therefore fetched
    as a plain navigation to a URL carrying a short-lived ``?grant=`` minted by
    ``POST .../files/download-grant`` instead of being buffered into a Blob to
    attach a Bearer header (issue #2352). Redeeming a grant also sets an httpOnly
    download cookie, which authenticates the browser's retry of an interrupted
    transfer once the grant's own window has closed (issue #2373). Every
    credential runs the same ``file:read`` gate, and the response is identical.

    **Probe** (issue #2383): a ``HEAD`` answers with the ``GET``'s status and
    headers and no body — a single file's ``Content-Length`` when it is known, and
    none at all for the incrementally built directory zip, exactly as the ``GET``
    declares them. It is the same endpoint behind the same gate; only the bytes
    and the audit record are skipped.
    """

    probing = request.method == "HEAD"
    try:
        is_dir = await use_case.is_dir(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
        )
        if is_dir:
            name = posixpath.basename(path.rstrip("/")) or "root"
            zip_headers = {
                "Content-Disposition": content_disposition(f"{name}.zip"),
                # A per-user body is never stored, whichever credential fetched
                # it (issue #2491): a cookie-authenticated request carries no
                # ``Authorization``, so RFC 9111 Section 3.5's default
                # protection from shared caches does not cover it.
                "Cache-Control": "no-store",
            }
            if probing:
                # The zip is built inside the stream ``dir_zip`` returns, so not
                # asking for it is the probe doing none of the download's work.
                response: Response = head_response(
                    media_type=_ZIP_MEDIA_TYPE, headers=zip_headers
                )
            else:
                stream = await use_case.dir_zip(
                    community_id=CommunityId(community_id),
                    server_id=ServerId(server_id),
                    rel_path=path,
                )
                response = StreamingResponse(
                    stream,
                    media_type=_ZIP_MEDIA_TYPE,
                    headers=zip_headers,
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
            name = posixpath.basename(path) or "download"
            headers = {
                "Content-Disposition": content_disposition(name),
                # Per-user body, same as the directory zip above.
                "Cache-Control": "no-store",
            }
            if size is not None:
                headers["Content-Length"] = str(size)
            if probing:
                # The size came from the cheap parent listing, so the probe
                # answers the length question without opening the download
                # stream. (``is_dir`` above still resolves the branch the way the
                # GET does, which for a file confirms readability by pulling one
                # chunk; the probe skips the download, not the dispatch.)
                response = head_response(media_type=_FILE_MEDIA_TYPE, headers=headers)
            else:
                file_stream = await use_case.file_stream(
                    community_id=CommunityId(community_id),
                    server_id=ServerId(server_id),
                    rel_path=path,
                )
                response = StreamingResponse(
                    file_stream,
                    media_type=_FILE_MEDIA_TYPE,
                    headers=headers,
                )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        # exc.reason, not a hardcoded invalid_path: a download is a READ, so it owes
        # the same answer the ``?path=`` read gives for the same path (issue #2432).
        # This route is at-rest only, so the reachable reasons are the escape's
        # invalid_path and Storage's symlink_refused.
        raise _unprocessable(exc.reason) from exc
    except ServerFilesUnsettledError as exc:
        if not probing:
            await _record_file_failure(
                recorder, ops.FILE_DOWNLOAD, authorized, community_id, server_id
            )
        raise _conflict("server_unsettled") from exc
    if not probing:
        # Deliberately unrecorded for a HEAD (issue #2383), here and in the
        # refusal above: a metadata probe is not a download, and recording one
        # identically would inflate the file:download counts.
        await _record_file(
            recorder, ops.FILE_DOWNLOAD, authorized, community_id, server_id
        )
    return response


class FileDownloadGrantResponse(BaseModel):
    """A short-lived, self-authenticating file download URL (issue #2352).

    ``download_url`` is same-origin relative (WEBUI_SPEC.md Section 7.7) and
    already carries the path and the grant, so a client hands it straight to
    ``<a download>``. ``expires_at`` is when the grant stops verifying; after that
    the URL is 401.
    """

    download_url: str
    expires_at: UtcDatetime


@router.post("/communities/{community_id}/servers/{server_id}/files/download-grant")
async def issue_file_download_grant(
    request: Request,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    response: Response,
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
    tokens: Annotated[TokenService, Depends(get_token_service)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
    path: Annotated[str, Query()] = _DEFAULT_DOWNLOAD_PATH,
) -> FileDownloadGrantResponse:
    """Mint a self-authenticating download URL for one path (file:read, #2352).

    A multi-GB directory zip cannot be buffered into a Blob just to attach a
    Bearer header, so the browser needs a URL that authenticates itself. The grant
    is bound to this exact community/server/path triple and to the caller, expires
    in ``auth.token.download_grant_ttl_seconds``, and proves identity only — the
    download re-runs the full ``file:read`` gate on redemption.

    ``path`` is a **query** parameter on this POST rather than a JSON body so the
    same ``_download_grant_resource`` callable builds the bound resource string
    here and at the download: divergence between the two is structurally
    impossible. The binding compares the decoded value by exact string equality;
    see :func:`file_download_grant_resource` for why containment — not a privilege
    boundary — is the right bar here.

    The pre-flight reuses ``DownloadFile.is_dir``, so a missing path is 404, a
    traversal-unsafe one 422 ``invalid_path``, one with a path-component symlink 422
    ``symlink_refused`` (issue #2432), and a running server 409
    ``server_unsettled`` — exactly what the download returns. The 409 records the
    DENIED ``file:download`` row the download would have recorded; once the Web UI
    mints first the download is never reached, and the denial would otherwise
    vanish from the audit log.

    Nothing is audited on success: bytes leave the system at redemption, which
    records ``file:download`` with this same subject as actor.
    """

    try:
        await use_case.is_dir(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            rel_path=path,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except ServerFileNotFoundError as exc:
        raise _not_found() from exc
    except InvalidFilePathError as exc:
        # The mint is where the Web UI meets a refused download path: since issue
        # #2352 it mints BEFORE downloading, so the download is never reached and
        # this is the operator-visible answer. It therefore forwards exc.reason for
        # the same reason the download does (issue #2432) — otherwise the browser
        # says "Invalid path" for a symlink the read route calls a symlink.
        raise _unprocessable(exc.reason) from exc
    except ServerFilesUnsettledError as exc:
        await _record_file_failure(
            recorder, ops.FILE_DOWNLOAD, authorized, community_id, server_id
        )
        raise _conflict("server_unsettled") from exc

    grant = tokens.issue_download_grant(
        IdentityUserId(authorized.user_id.value), _download_grant_resource(request)
    )
    # The body hands back a credential-bearing URL, so no cache may keep it
    # (issue #2491).
    response.headers["Cache-Control"] = "no-store"
    return FileDownloadGrantResponse(
        download_url=(
            f"/api/communities/{community_id}/servers/{server_id}/files/download"
            f"?path={quote(path, safe='')}&grant={quote(grant.token, safe='')}"
        ),
        expires_at=grant.expires_at,
    )


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
    path is traversal-validated (422); the root path is rejected since the root
    always exists (issue #1944). Both backends materialize the directory
    (fs: real empty directory; object storage: zero-byte ``.dir`` marker,
    issue #1125).
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
