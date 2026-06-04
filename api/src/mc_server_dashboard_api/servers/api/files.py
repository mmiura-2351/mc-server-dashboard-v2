"""HTTP edge for server file management (Section 6.10), with state branching.

Routes live under ``/communities/{community_id}/servers/{server_id}/files`` and
are *per-resource* gated (``resource_type='server'``,
``resource_id_param='server_id'``) like the server routes: a grant on one server
opens exactly that server's files (FR-AUTHZ-2). The catalog codes are ``file:read``
(browse + read), ``file:edit`` (write), ``file:history`` (versions), and
``file:rollback``.

File content is carried base64-encoded in JSON so reads/writes are bytes-faithful
(the proto fields are ``bytes``; no text/encoding mangling). The router is thin:
it resolves use cases via DI, runs them, and maps the servers file errors to HTTP
codes (404 keeps the no-existence-signal posture; a traversal-unsafe path is 422;
an oversized edit is 413; a transitional server is 409; a disconnected worker is
503).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from mc_server_dashboard_api.community.domain.value_objects import Permission
from mc_server_dashboard_api.dependencies import (
    get_list_dir,
    get_list_file_versions,
    get_read_file,
    get_rollback_file,
    get_write_file,
    require_permission,
)
from mc_server_dashboard_api.servers.application.files import (
    ListDir,
    ListFileVersions,
    ReadFile,
    RollbackFile,
    WriteFile,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    FileTooLargeError,
    InvalidFilePathError,
    ServerFileNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileEntry
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"


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
            raise _unprocessable("invalid_path") from exc
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
        raise _unprocessable("invalid_path") from exc
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
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("file:edit"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[WriteFile, Depends(get_write_file)],
    path: Annotated[str, Query()] = ".",
) -> None:
    """Edit a file, branching on server state (Section 6.9)."""

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
        raise _unprocessable("invalid_path") from exc
    except FileTooLargeError as exc:
        raise _too_large() from exc
    except ServerFilesUnsettledError as exc:
        raise _conflict("server_unsettled") from exc
    except WorkerUnavailableError as exc:
        raise _service_unavailable("worker_unavailable") from exc
    except CommandDispatchError as exc:
        raise _conflict("command_failed") from exc


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
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("file:rollback"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[RollbackFile, Depends(get_rollback_file)],
    path: Annotated[str, Query()],
) -> None:
    """Roll a file back to a retained version (file:rollback).

    Requires the server at rest (Section 6.9): rollback republishes the
    authoritative copy, so it is 409 while running.
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
        raise _conflict("server_not_stopped") from exc


def _decode(content_base64: str) -> bytes:
    try:
        return base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _unprocessable("invalid_base64") from exc


def _unprocessable(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"reason": reason},
    )


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={"reason": "file_too_large"},
    )


def _service_unavailable(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"reason": reason},
    )


def _conflict(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"reason": reason},
    )


def _not_found() -> HTTPException:
    # Keep the no-existence-signal posture (Section 6.4): a server/file outside
    # this community 404s the same as a wholly unknown one.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
