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

import logging
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import AuditEvent, Outcome
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.community.domain.value_objects import AuthUser, Permission
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_clear_backup_retention,
    get_create_backup,
    get_delete_backup,
    get_download_backup,
    get_global_backup_statistics,
    get_list_backups,
    get_restore_backup,
    get_server_backup_statistics,
    get_set_backup_retention,
    get_upload_backup,
    require_permission,
    require_platform_admin,
)
from mc_server_dashboard_api.http_datetime import UtcDatetime
from mc_server_dashboard_api.http_problem import ProblemException, problem
from mc_server_dashboard_api.servers.application.backups import (
    ClearBackupRetention,
    CreateBackup,
    DeleteBackup,
    DownloadBackup,
    GlobalBackupStatistics,
    ListBackups,
    ListedBackup,
    RestoreBackup,
    ServerBackupStatistics,
    SetBackupRetention,
    UploadBackup,
)
from mc_server_dashboard_api.servers.application.files import MAX_UPLOAD_BYTES
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupId,
    BackupSource,
    BackupStatistics,
)
from mc_server_dashboard_api.servers.domain.backup_retention import RetentionPolicy
from mc_server_dashboard_api.servers.domain.control_plane import (
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    BackupNotFoundError,
    BackupUnsettledError,
    CommandDispatchError,
    FileTooLargeError,
    InvalidBackupArchiveError,
    InvalidRetentionPolicyError,
    ServerBusyError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

router = APIRouter()

_logger = logging.getLogger(__name__)

_SERVER_RESOURCE_TYPE = "server"

# How much to pull per chunk when buffering the multipart upload, so an over-cap
# body is refused as soon as the running count crosses MAX_UPLOAD_BYTES rather
# than materializing the whole part first (mirroring the files upload edge).
_UPLOAD_CHUNK_BYTES = 1024 * 1024

# Backups are self-contained gzip tar archives (STORAGE.md Section 2); download
# streams the native bytes with this content type and a ``.tar.gz`` attachment.
_BACKUP_MEDIA_TYPE = "application/gzip"


class BackupResponse(BaseModel):
    """One backup's metadata (DATABASE.md Section 8)."""

    id: uuid.UUID
    server_id: uuid.UUID
    source: str
    # Structural health of the archived contents (issue #742): ``healthy`` (gated
    # create path), ``unknown`` (legacy/uploaded, not yet checked), ``quarantined``.
    health: str
    size_bytes: int | None
    created_by: uuid.UUID | None
    # The author's display username, resolved server-side at read time (issue #688).
    # ``None`` when the backup has no actor (scheduled) or the author no longer
    # resolves (deleted user) — the client then falls back to the raw ``created_by``.
    # Only the listing resolves it; the single-create responses leave it ``None``.
    created_by_username: str | None = None
    created_at: UtcDatetime

    @classmethod
    def from_backup(cls, backup: Backup) -> "BackupResponse":
        return cls(
            id=backup.id.value,
            server_id=backup.server_id.value,
            source=backup.source.value,
            health=backup.health.value,
            size_bytes=backup.size_bytes,
            created_by=backup.created_by,
            created_at=backup.created_at,
        )

    @classmethod
    def from_listed(cls, listed: ListedBackup) -> "BackupResponse":
        return cls(
            id=listed.backup.id.value,
            server_id=listed.backup.server_id.value,
            source=listed.backup.source.value,
            health=listed.backup.health.value,
            size_bytes=listed.backup.size_bytes,
            created_by=listed.backup.created_by,
            created_by_username=listed.created_by_username,
            created_at=listed.backup.created_at,
        )


class BackupListResponse(BaseModel):
    backups: list[BackupResponse]


class BackupStatisticsResponse(BaseModel):
    """Backup usage for a scope (one server, or the whole platform; issue #281).

    ``total_bytes`` sums only the rows with a recorded ``size_bytes``;
    ``unknown_size_count`` is the number of legacy NULL-size rows excluded from it.
    """

    count: int
    total_bytes: int
    unknown_size_count: int
    newest: UtcDatetime | None
    oldest: UtcDatetime | None

    @classmethod
    def from_stats(cls, stats: BackupStatistics) -> "BackupStatisticsResponse":
        return cls(
            count=stats.count,
            total_bytes=stats.total_bytes,
            unknown_size_count=stats.unknown_size_count,
            newest=stats.newest,
            oldest=stats.oldest,
        )


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
        await _record_failure(
            recorder,
            ops.BACKUP_CREATE,
            Outcome.DENIED,
            authorized,
            community_id,
            server_id,
        )
        raise _conflict("server_unsettled") from exc
    except ServerBusyError as exc:
        # A concurrent lifecycle op held the per-server lock past the acquire
        # budget (issue #876): a transient 409 the caller retries.
        await _record_failure(
            recorder,
            ops.BACKUP_CREATE,
            Outcome.DENIED,
            authorized,
            community_id,
            server_id,
        )
        raise _conflict("server_busy") from exc
    except WorkerUnavailableError as exc:
        await _record_failure(
            recorder,
            ops.BACKUP_CREATE,
            Outcome.ERROR,
            authorized,
            community_id,
            server_id,
        )
        raise _service_unavailable("worker_unavailable") from exc
    except CommandDispatchError as exc:
        await _record_failure(
            recorder,
            ops.BACKUP_CREATE,
            Outcome.DENIED,
            authorized,
            community_id,
            server_id,
        )
        raise _conflict("command_failed") from exc
    except BackupCorruptError as exc:
        # The working set is structurally corrupt (a crash-during-save truncation,
        # #703): the integrity gate refused to archive it (#739). This is a
        # server-side data integrity failure, not a client error, so it is a 500
        # with a machine-readable reason; the refusal is logged and audited with the
        # corrupt-file count so an operator can see why no backup was produced.
        _logger.warning(
            "backup create refused: corrupt working set for server %s "
            "(%d corrupt region file(s))",
            server_id,
            exc.corrupt_count,
        )
        await _record_failure(
            recorder,
            ops.BACKUP_CREATE,
            Outcome.ERROR,
            authorized,
            community_id,
            server_id,
        )
        raise _integrity_error("working_set_corrupt") from exc
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
    return BackupListResponse(backups=[BackupResponse.from_listed(b) for b in backups])


class RetentionPolicyBody(BaseModel):
    """The scheduled-backup retention policy (issue #1841).

    Exactly one form: keep-N (``keep_last`` >= 1, tiers omitted) or tiered
    (``daily`` / ``weekly`` / ``monthly`` each >= 0, at least one > 0,
    ``keep_last`` omitted). The use case validates; a violation is 422
    ``invalid_retention_policy``. The same shape is the response of a
    successful PUT.
    """

    keep_last: int | None = None
    daily: int | None = None
    weekly: int | None = None
    monthly: int | None = None

    @classmethod
    def from_policy(cls, policy: RetentionPolicy) -> "RetentionPolicyBody":
        if policy.keep_last is not None:
            return cls(keep_last=policy.keep_last)
        return cls(daily=policy.daily, weekly=policy.weekly, monthly=policy.monthly)


# NOTE: the retention routes are registered BEFORE the ``/{backup_id}`` routes
# so the literal ``retention`` segment is never captured by the UUID path
# parameter (FastAPI matches in registration order).


@router.put("/communities/{community_id}/servers/{server_id}/backups/retention")
async def set_backup_retention(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    body: RetentionPolicyBody,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("backup:schedule"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[SetBackupRetention, Depends(get_set_backup_retention)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> RetentionPolicyBody:
    """Set the scheduled-backup retention policy (backup:schedule, issue #1841).

    The policy is `{keep_last}` XOR `{daily, weekly, monthly}`; it applies only
    to `source=scheduled` backups — manual/uploaded rows are never auto-deleted.
    Setting it prunes immediately (best-effort), and every successful scheduled
    backup run prunes thereafter; each pruned backup is audited as
    `backup:delete` with no actor. The write itself is audited as
    `backup:set_retention` with the acting user, so the causal actor behind
    those actor-less prune rows stays recoverable. The policy is readable as
    `backup_retention` on the server read. An invalid shape is 422
    `invalid_retention_policy`.
    """

    try:
        policy = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            keep_last=body.keep_last,
            daily=body.daily,
            weekly=body.weekly,
            monthly=body.monthly,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except InvalidRetentionPolicyError as exc:
        raise _unprocessable("invalid_retention_policy") from exc
    await _record(
        recorder,
        ops.BACKUP_SET_RETENTION,
        authorized,
        community_id,
        server_id,
        target_type=ops.TARGET_SERVER,
    )
    return RetentionPolicyBody.from_policy(policy)


@router.delete(
    "/communities/{community_id}/servers/{server_id}/backups/retention",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def clear_backup_retention(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("backup:schedule"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[ClearBackupRetention, Depends(get_clear_backup_retention)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> None:
    """Clear the retention policy (backup:schedule): scheduled backups then
    accumulate unbounded again. Nothing is pruned on clear; the write is
    audited as `backup:clear_retention` with the acting user."""

    try:
        await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    await _record(
        recorder,
        ops.BACKUP_CLEAR_RETENTION,
        authorized,
        community_id,
        server_id,
        target_type=ops.TARGET_SERVER,
    )


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
    force: bool = False,
) -> None:
    """Restore a backup; requires the server stopped (FR-BAK-4 -> 409 if running).

    The restore validates the extracted backup against the integrity gate (#743): a
    corrupt backup is refused with 500 ``working_set_corrupt`` (the use case has
    quarantined it). ``?force=true`` is the operator override — it publishes a
    known-corrupt backup anyway (#703), records a distinct ``backup:force_restore``
    audit entry naming who forced it and the corrupt count, and quarantines it. The
    create-direction gate (#749) has no such override.
    """

    try:
        result = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            backup_id=BackupId(backup_id),
            force=force,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except BackupNotFoundError as exc:
        raise _not_found() from exc
    except ServerNotStoppedError as exc:
        await _record_failure(
            recorder,
            ops.BACKUP_RESTORE,
            Outcome.DENIED,
            authorized,
            community_id,
            backup_id,
            target_type=ops.TARGET_BACKUP,
        )
        raise _conflict("server_not_stopped") from exc
    except ServerBusyError as exc:
        # A concurrent lifecycle op held the per-server lock past the acquire
        # budget (issue #876): a transient 409 the caller retries.
        await _record_failure(
            recorder,
            ops.BACKUP_RESTORE,
            Outcome.DENIED,
            authorized,
            community_id,
            backup_id,
            target_type=ops.TARGET_BACKUP,
        )
        raise _conflict("server_busy") from exc
    except BackupCorruptError as exc:
        # The backup's extracted working set is structurally corrupt and force was
        # not set (#743): the restore was refused and the backup quarantined. Like
        # the create-direction gate (#749), this is a server-side data fault, not a
        # client error -> 500 with a machine-readable reason; the refusal is logged
        # and audited with the corrupt-file count.
        _logger.warning(
            "backup restore refused: corrupt backup %s for server %s "
            "(%d corrupt region file(s))",
            backup_id,
            server_id,
            exc.corrupt_count,
        )
        await _record_failure(
            recorder,
            ops.BACKUP_RESTORE,
            Outcome.ERROR,
            authorized,
            community_id,
            backup_id,
            target_type=ops.TARGET_BACKUP,
        )
        raise _integrity_error("working_set_corrupt") from exc
    if result.forced_corrupt:
        # An operator forced the restore of a known-corrupt backup over the gate
        # (#703): it published. Log and audit the deliberate corrupt restore under a
        # distinct operation so it is queryable apart from a routine restore.
        _logger.warning(
            "backup force-restore: operator %s published known-corrupt backup %s "
            "for server %s (%d corrupt region file(s))",
            authorized.user_id.value,
            backup_id,
            server_id,
            result.corrupt_count,
        )
        await _record(
            recorder, ops.BACKUP_FORCE_RESTORE, authorized, community_id, backup_id
        )
        return
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
    except ServerBusyError as exc:
        # A concurrent lifecycle op held the per-server lock past the acquire
        # budget (issue #876): a transient 409 the caller retries.
        raise _conflict("server_busy") from exc
    await _record(recorder, ops.BACKUP_DELETE, authorized, community_id, backup_id)


@router.get(
    "/communities/{community_id}/servers/{server_id}/backups/{backup_id}/download",
)
async def download_backup(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    backup_id: uuid.UUID,
    authorized: Annotated[
        AuthUser,
        Depends(
            require_permission(
                Permission("backup:read"),
                resource_type=_SERVER_RESOURCE_TYPE,
                resource_id_param="server_id",
            )
        ),
    ],
    use_case: Annotated[DownloadBackup, Depends(get_download_backup)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> StreamingResponse:
    """Stream a backup archive in its native ``tar.gz`` format (backup:read, #281).

    No recompression: the exact stored bytes stream out with a ``.tar.gz``
    attachment. An unknown / cross-community backup is 404 (no existence signal).
    """

    try:
        stream = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            backup_id=BackupId(backup_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except BackupNotFoundError as exc:
        raise _not_found() from exc
    await _record(recorder, ops.BACKUP_DOWNLOAD, authorized, community_id, backup_id)
    return StreamingResponse(
        stream,
        media_type=_BACKUP_MEDIA_TYPE,
        headers={"Content-Disposition": _content_disposition(f"{backup_id}.tar.gz")},
    )


@router.post(
    "/communities/{community_id}/servers/{server_id}/backups/upload",
    status_code=status.HTTP_201_CREATED,
)
async def upload_backup(
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    file: UploadFile,
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
    use_case: Annotated[UploadBackup, Depends(get_upload_backup)],
    recorder: Annotated[AuditRecorder, Depends(get_audit_recorder)],
) -> BackupResponse:
    """Upload an off-host backup archive as a new restorable backup (backup:create).

    The multipart body is buffered with the chunked pre-buffer cap (over-cap -> 413
    before the whole body is materialized); the use case then validates the archive
    (opens + traversal-safe entries) before storing it. A non-archive / unsafe
    member is 422; an over-cap archive is 413.
    """

    content = await _read_capped_upload(file)
    try:
        backup = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
            content=content,
            created_by=authorized.user_id.value,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except FileTooLargeError as exc:
        raise _too_large() from exc
    except InvalidBackupArchiveError as exc:
        raise _unprocessable("invalid_archive") from exc
    await _record(
        recorder, ops.BACKUP_UPLOAD, authorized, community_id, backup.id.value
    )
    return BackupResponse.from_backup(backup)


@router.get(
    "/communities/{community_id}/servers/{server_id}/backups/statistics",
)
async def server_backup_statistics(
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
    use_case: Annotated[ServerBackupStatistics, Depends(get_server_backup_statistics)],
) -> BackupStatisticsResponse:
    """A server's backup usage: count, bytes, newest/oldest (backup:read, #281)."""

    try:
        stats = await use_case(
            community_id=CommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    return BackupStatisticsResponse.from_stats(stats)


@router.get("/backups/statistics", dependencies=[Depends(require_platform_admin)])
async def global_backup_statistics(
    use_case: Annotated[GlobalBackupStatistics, Depends(get_global_backup_statistics)],
) -> BackupStatisticsResponse:
    """Platform-wide backup usage (platform-admin axis, issue #281).

    The smallest honest global shape: total count, summed known bytes, the
    NULL-size (unknown) count, and the newest/oldest timestamps across the whole
    platform. Gated by the platform-admin flag (non-admin -> 403), like /workers.
    """

    stats = await use_case()
    return BackupStatisticsResponse.from_stats(stats)


async def _read_capped_upload(file: UploadFile) -> bytes:
    """Pull the multipart body in chunks, aborting with 413 past the upload cap.

    Reading in bounded chunks and checking the running count after each lets an
    over-cap upload be refused as soon as the count crosses MAX_UPLOAD_BYTES,
    rather than materializing a body far larger than the cap (mirroring the files
    upload edge). The use case re-checks the cap, so this is the edge's early-out.
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

    Emits an ASCII-only ``filename`` fallback plus an RFC 5987 ``filename*`` with
    the UTF-8 percent-encoded original, the #262 hardening (a crafted name cannot
    inject extra header params or 500 on a non-latin-1 char). Backup names are
    UUIDs, so this is straightforward here, but the helper keeps the same posture
    as the files download edge.
    """

    ascii_fallback = "".join(
        c if (0x20 <= ord(c) < 0x7F and c not in '"\\') else "_" for c in filename
    )
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


async def _record(
    recorder: AuditRecorder,
    operation: str,
    authorized: AuthUser,
    community_id: uuid.UUID,
    target_id: uuid.UUID,
    *,
    target_type: str = ops.TARGET_BACKUP,
) -> None:
    """Record a successful backup operation (FR-AUD-1), fire-after-commit.

    Most operations target the backup; the retention policy writes (issue
    #1841) target the server the policy hangs on.
    """

    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=Outcome.SUCCESS,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=target_type,
            target_id=target_id,
        )
    )


async def _record_failure(
    recorder: AuditRecorder,
    operation: str,
    outcome: Outcome,
    authorized: AuthUser,
    community_id: uuid.UUID,
    target_id: uuid.UUID,
    *,
    target_type: str = ops.TARGET_SERVER,
) -> None:
    """Record a failed privileged backup operation (issue #131; FR-AUD-1).

    A refused attempt (``DENIED`` — server unsettled / not stopped / dispatch
    refused) or a transient fleet failure (``ERROR`` — worker unreachable). A
    create failure has no backup id yet, so it targets the server; a restore
    failure targets the backup.
    """

    await recorder.record(
        AuditEvent(
            operation=operation,
            outcome=outcome,
            actor_id=authorized.user_id.value,
            community_id=community_id,
            target_type=target_type,
            target_id=target_id,
        )
    )


def _service_unavailable(reason: str) -> ProblemException:
    return problem(status.HTTP_503_SERVICE_UNAVAILABLE, reason)


def _integrity_error(reason: str) -> ProblemException:
    # A structurally corrupt working set is a server-side data fault, not a client
    # error (issue #739): a 500 with a machine-readable reason, not a 4xx.
    return problem(status.HTTP_500_INTERNAL_SERVER_ERROR, reason)


def _conflict(reason: str) -> ProblemException:
    return problem(status.HTTP_409_CONFLICT, reason)


def _not_found() -> ProblemException:
    # Keep the no-existence-signal posture (Section 6.4): a server/backup outside
    # this community 404s the same as a wholly unknown one.
    return problem(status.HTTP_404_NOT_FOUND, "not_found")


def _too_large() -> ProblemException:
    return problem(status.HTTP_413_CONTENT_TOO_LARGE, "too_large")


def _unprocessable(reason: str) -> ProblemException:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, reason)
