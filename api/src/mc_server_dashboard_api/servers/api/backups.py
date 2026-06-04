"""HTTP edge for server backup management (Section 6.11), with state branching.

Routes live under ``/communities/{community_id}/servers/{server_id}/backups`` and
are *per-resource* gated (``resource_type='server'``,
``resource_id_param='server_id'``) like the server and file routes: a grant on one
server opens exactly that server's backups (FR-AUTHZ-2). The catalog codes are
``backup:create``, ``backup:read`` (list), ``backup:restore``, and
``backup:delete``.

The router is thin: it resolves use cases via DI, runs them, and maps the servers
backup errors to HTTP codes (404 keeps the no-existence-signal posture; a server in
a transitional state for create is 409; restore against a running server is 409
per FR-BAK-4; a worker that cannot be reached on the running create path is 503; a
working set with nothing to archive is 404).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_create_backup,
    get_delete_backup,
    get_list_backups,
    get_restore_backup,
    require_permission,
)
from mc_server_dashboard_api.servers.application.backups import (
    CreateBackup,
    DeleteBackup,
    ListBackups,
    RestoreBackup,
)
from mc_server_dashboard_api.servers.domain.backup import Backup, BackupId, BackupSource
from mc_server_dashboard_api.servers.domain.control_plane import (
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.errors import (
    BackupNotFoundError,
    BackupUnsettledError,
    CommandDispatchError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"


class BackupResponse(BaseModel):
    """One backup's metadata (DATABASE.md Section 8)."""

    id: uuid.UUID
    server_id: uuid.UUID
    source: str
    size_bytes: int | None
    created_by: uuid.UUID | None
    created_at: str

    @classmethod
    def from_backup(cls, backup: Backup) -> "BackupResponse":
        return cls(
            id=backup.id.value,
            server_id=backup.server_id.value,
            source=backup.source.value,
            size_bytes=backup.size_bytes,
            created_by=backup.created_by,
            created_at=backup.created_at.isoformat(),
        )


class BackupListResponse(BaseModel):
    backups: list[BackupResponse]


@router.post(
    "/communities/{community_id}/servers/{server_id}/backups",
    status_code=status.HTTP_201_CREATED,
)
async def create_backup(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("backup:create"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[CreateBackup, Depends(get_create_backup)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> BackupResponse:
    """Create a backup, branching on server state (Section 6.9, FR-BAK-2)."""

    try:
        backup = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            source=BackupSource.MANUAL,
            created_by=authorized.user_id.value,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except BackupNotFoundError as exc:
        # Nothing published to archive (no working set yet).
        raise _not_found() from exc
    except BackupUnsettledError as exc:
        raise _conflict("server_unsettled") from exc
    except WorkerUnavailableError as exc:
        raise _service_unavailable("worker_unavailable") from exc
    except CommandDispatchError as exc:
        raise _conflict("command_failed") from exc
    await _record(
        recorder, ops.BACKUP_CREATE, authorized, community_id, backup.id.value
    )
    return BackupResponse.from_backup(backup)


@router.get("/communities/{community_id}/servers/{server_id}/backups")
async def list_backups(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    _authorized: Annotated[
        object,
        Depends(
            require_permission(
                Permission("backup:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[ListBackups, Depends(get_list_backups)],
) -> BackupListResponse:
    """List a server's backups newest-first (backup:read)."""

    try:
        backups = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    return BackupListResponse(backups=[BackupResponse.from_backup(b) for b in backups])


@router.post(
    "/communities/{community_id}/servers/{server_id}/backups/{backup_id}/restore",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def restore_backup(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    backup_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("backup:restore"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[RestoreBackup, Depends(get_restore_backup)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Restore a backup; requires the server stopped (FR-BAK-4 -> 409 if running)."""

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            backup_id=BackupId(backup_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except BackupNotFoundError as exc:
        raise _not_found() from exc
    except ServerNotStoppedError as exc:
        raise _conflict("server_not_stopped") from exc
    await _record(recorder, ops.BACKUP_RESTORE, authorized, community_id, backup_id)


@router.delete(
    "/communities/{community_id}/servers/{server_id}/backups/{backup_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_backup(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    backup_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("backup:delete"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[DeleteBackup, Depends(get_delete_backup)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Delete a backup's archive then its metadata row (backup:delete)."""

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            backup_id=BackupId(backup_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except BackupNotFoundError as exc:
        raise _not_found() from exc
    await _record(recorder, ops.BACKUP_DELETE, authorized, community_id, backup_id)


async def _record(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    backup_id: uuid.UUID,
) -> None:
    """Record a successful backup operation (FR-AUD-1), fire-after-commit."""

    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=ops.TARGET_BACKUP,
            target_id=backup_id,
        )
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
    # Keep the no-existence-signal posture (Section 6.4): a server/backup outside
    # this community 404s the same as a wholly unknown one.
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
