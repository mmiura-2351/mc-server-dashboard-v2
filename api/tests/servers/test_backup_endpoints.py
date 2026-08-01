"""Endpoint tests for the server-backups router (Section 6.11).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate per route (non-member -> 404, member-without-permission ->
  403, authorized member -> 2xx);
- the servers-backup-error -> HTTP-code mapping (missing 404, unsettled 409,
  restore-running 409, worker-down 503, store-down 503 on every route that
  reaches the object store before a response body starts);
- create records the acting user (created_by passed through);
- list shape.
"""

from __future__ import annotations

import datetime as dt
import inspect
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_authenticate_download_grant,
    get_authenticate_request,
    get_clear_backup_retention,
    get_create_backup,
    get_current_user,
    get_delete_backup,
    get_download_backup,
    get_global_backup_statistics,
    get_list_backups,
    get_membership_visibility,
    get_permission_checker,
    get_resolve_backup,
    get_restore_backup,
    get_server_backup_statistics,
    get_set_backup_retention,
    get_settings,
    get_token_service,
    get_upload_backup,
)
from mc_server_dashboard_api.download_cookie import DOWNLOAD_COOKIE_NAME
from mc_server_dashboard_api.http_streaming import ShortResponseBodyError
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.application.authenticate_download_grant import (
    AuthenticateDownloadGrant,
)
from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.application.backups import (
    DownloadBackup,
    ListedBackup,
    RestoreResult,
    download_grant_resource,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
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
    BackupStorageUnavailableError,
    BackupUnsettledError,
    CommandDispatchError,
    FileTooLargeError,
    InvalidBackupArchiveError,
    InvalidRetentionPolicyError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.audit.fakes import RecordingAuditRecorder
from tests.identity.fakes import FakeClock, FakeUnitOfWork, make_user

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)

# A 32-byte HS256 key; the value is irrelevant, only that mint and verify share it.
_SIGNING_KEY = "0123456789abcdef0123456789abcdef"
_GRANT_TTL_SECONDS = 30
_COOKIE_TTL_SECONDS = 900


class _FakeVisibility(MembershipVisibility):
    def __init__(self, *, member: bool) -> None:
        self._member = member

    async def is_member(self, *, user_id: UserId, community_id: CommunityId) -> bool:
        return self._member


class _FakeChecker(PermissionChecker):
    def __init__(self, *, allow: bool) -> None:
        self._allow = allow

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        return self._allow


class _FakeUseCase:
    def __init__(self, *, result: object = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


def _backup(server_id: ServerId) -> Backup:
    return Backup(
        id=BackupId(uuid.uuid4()),
        server_id=server_id,
        storage_ref="ref",
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=uuid.uuid4(),
        created_at=_NOW,
    )


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


# Set by _app so the download tests can mint real access tokens and grants against
# the same signing key and clock the app under test verifies with.
_clock: FakeClock
_tokens: JwtTokenService
_user: User


def _app(
    *,
    member: bool,
    allow: bool,
    subject: User | None = None,
    resolve: _FakeUseCase | None = None,
    create: _FakeUseCase | None = None,
    list_: _FakeUseCase | None = None,
    restore: _FakeUseCase | None = None,
    delete: _FakeUseCase | None = None,
    download: _FakeDownload | None = None,
    upload: _FakeUseCase | None = None,
    statistics: _FakeUseCase | None = None,
    global_statistics: _FakeUseCase | None = None,
    set_retention: _FakeUseCase | None = None,
    clear_retention: _FakeUseCase | None = None,
    recorder: RecordingAuditRecorder | None = None,
    is_admin: bool = False,
) -> object:
    global _clock, _tokens, _user
    app = _shared_app
    app.dependency_overrides.clear()
    _user = subject if subject is not None else make_user(is_platform_admin=is_admin)
    _clock = FakeClock(_NOW)
    _tokens = JwtTokenService(
        signing_key=_SIGNING_KEY,
        algorithm="HS256",
        access_ttl=dt.timedelta(minutes=15),
        download_grant_ttl=dt.timedelta(seconds=_GRANT_TTL_SECONDS),
        download_cookie_ttl=dt.timedelta(seconds=_COOKIE_TTL_SECONDS),
        clock=_clock,
    )
    identity_uow = FakeUnitOfWork()
    identity_uow.users.seed(_user)
    app.dependency_overrides[get_current_user] = lambda: _user
    # The download route resolves its subject itself (Bearer *or* grant), so it
    # goes through these two use cases rather than get_current_user.
    app.dependency_overrides[get_authenticate_request] = lambda: AuthenticateRequest(
        uow=identity_uow, tokens=_tokens
    )
    app.dependency_overrides[get_authenticate_download_grant] = lambda: (
        AuthenticateDownloadGrant(uow=identity_uow, tokens=_tokens)
    )
    app.dependency_overrides[get_token_service] = lambda: _tokens
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if resolve is not None:
        app.dependency_overrides[get_resolve_backup] = lambda: resolve
    if create is not None:
        app.dependency_overrides[get_create_backup] = lambda: create
    if list_ is not None:
        app.dependency_overrides[get_list_backups] = lambda: list_
    if restore is not None:
        app.dependency_overrides[get_restore_backup] = lambda: restore
    if delete is not None:
        app.dependency_overrides[get_delete_backup] = lambda: delete
    if download is not None:
        app.dependency_overrides[get_download_backup] = lambda: download
    if upload is not None:
        app.dependency_overrides[get_upload_backup] = lambda: upload
    if statistics is not None:
        app.dependency_overrides[get_server_backup_statistics] = lambda: statistics
    if global_statistics is not None:
        app.dependency_overrides[get_global_backup_statistics] = lambda: (
            global_statistics
        )
    if set_retention is not None:
        app.dependency_overrides[get_set_backup_retention] = lambda: set_retention
    if clear_retention is not None:
        app.dependency_overrides[get_clear_backup_retention] = lambda: clear_retention
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return app


def _stats() -> BackupStatistics:
    return BackupStatistics(
        count=2,
        total_bytes=30,
        unknown_size_count=1,
        newest=_NOW,
        oldest=_NOW,
    )


def _url(community: uuid.UUID, server: uuid.UUID, suffix: str = "") -> str:
    return f"/api/communities/{community}/servers/{server}/backups{suffix}"


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_create() -> None:
    app = _app(member=False, allow=True, create=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_create() -> None:
    app = _app(member=True, allow=False, create=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 403


def test_member_without_permission_gets_403_on_delete() -> None:
    app = _app(member=True, allow=False, delete=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}"))
    assert resp.status_code == 403


# --- create ----------------------------------------------------------------


def test_create_returns_201_and_passes_actor() -> None:
    server = ServerId(uuid.uuid4())
    use_case = _FakeUseCase(result=_backup(server))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, create=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), server.value))
    assert resp.status_code == 201
    body = resp.json()
    assert body["source"] == "manual"
    # The backup's health is surfaced in the create response (issue #742).
    assert body["health"] == "healthy"
    # The authorized actor is forwarded as created_by, and source is MANUAL.
    assert use_case.calls[0]["source"] is BackupSource.MANUAL
    assert isinstance(use_case.calls[0]["created_by"], uuid.UUID)
    # A successful create records a backup:create SUCCESS against the new backup.
    assert [e.operation for e in recorder.events] == [ops.BACKUP_CREATE]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_BACKUP


def test_create_unsettled_is_409() -> None:
    use_case = _FakeUseCase(error=BackupUnsettledError("x"))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, create=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 409
    # A create refused because the server is unsettled records backup:create
    # DENIED against the server (no backup id yet).
    assert [e.operation for e in recorder.events] == [ops.BACKUP_CREATE]
    assert recorder.events[0].outcome is Outcome.DENIED
    assert recorder.events[0].target_type == ops.TARGET_SERVER


def test_create_nothing_to_archive_is_404() -> None:
    use_case = _FakeUseCase(error=BackupNotFoundError("x"))
    app = _app(member=True, allow=True, create=use_case)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 404


def test_create_worker_unavailable_is_503() -> None:
    use_case = _FakeUseCase(error=WorkerUnavailableError("x"))
    app = _app(member=True, allow=True, create=use_case)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 503


def test_create_storage_unavailable_is_503_with_reason() -> None:
    # The object store was down mid-archive (issue #2378): a transient backend
    # fault, so 503 storage_unavailable — not a generic 500 — telling the client
    # to retry and keeping genuine 500s meaningful in monitoring.
    use_case = _FakeUseCase(error=BackupStorageUnavailableError("x"))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, create=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 503
    assert resp.json()["reason"] == "storage_unavailable"
    # No Retry-After: nothing in the request path knows when the store recovers.
    assert "Retry-After" not in resp.headers
    # Mirrors the worker-down path: an ERROR against the server (no backup id).
    assert [e.operation for e in recorder.events] == [ops.BACKUP_CREATE]
    assert recorder.events[0].outcome is Outcome.ERROR
    assert recorder.events[0].target_type == ops.TARGET_SERVER


def test_create_dispatch_failure_keeps_the_sanitized_reason() -> None:
    # The running-server create dispatches a SnapshotTrigger, which the Worker
    # refuses with BUSY when another mutating command for the id is already in
    # flight (proto/contract/command_error_contract.json, SnapshotTrigger /
    # command_in_flight). command_dispatch.py sanitizes that to ``worker_busy``;
    # the edge must pass it through instead of flattening it to the catch-all,
    # so the client can tell a retryable contention apart from a real failure
    # (issue #2436).
    use_case = _FakeUseCase(error=CommandDispatchError("busy", reason="worker_busy"))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, create=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "worker_busy"
    assert [e.operation for e in recorder.events] == [ops.BACKUP_CREATE]
    assert recorder.events[0].outcome is Outcome.DENIED
    assert recorder.events[0].target_type == ops.TARGET_SERVER


def test_create_unclassified_dispatch_failure_is_command_failed() -> None:
    # A dispatch failure the Worker did not classify (no sanitized reason) keeps
    # the catch-all so the reason stays a closed set (issue #2436).
    use_case = _FakeUseCase(error=CommandDispatchError("boom"))
    app = _app(member=True, allow=True, create=use_case)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "command_failed"


def test_create_corrupt_working_set_is_500_with_reason() -> None:
    # The integrity gate (#739) refused to archive a structurally corrupt working
    # set: a server-side data fault, surfaced as a 500 with a machine-readable
    # reason, not a 4xx client error.
    use_case = _FakeUseCase(error=BackupCorruptError("x", corrupt_count=3))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, create=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 500
    assert resp.json()["reason"] == "working_set_corrupt"
    # The refused-by-gate create records backup:create ERROR; with no backup id
    # yet it targets the server.
    assert [e.operation for e in recorder.events] == [ops.BACKUP_CREATE]
    assert recorder.events[0].outcome is Outcome.ERROR
    assert recorder.events[0].target_type == ops.TARGET_SERVER


# --- list ------------------------------------------------------------------


def test_list_returns_backups() -> None:
    server = ServerId(uuid.uuid4())
    backup = _backup(server)
    use_case = _FakeUseCase(
        result=[ListedBackup(backup=backup, created_by_username="alice")]
    )
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), server.value))
    assert resp.status_code == 200
    backups = resp.json()["backups"]
    assert len(backups) == 1
    # The list response carries each backup's health (issue #742).
    assert backups[0]["health"] == "healthy"
    # The author's resolved username is surfaced (issue #688); the raw id stays.
    assert backups[0]["created_by_username"] == "alice"
    assert backups[0]["created_by"] == str(backup.created_by)


def test_list_unresolved_author_username_is_null() -> None:
    # A deleted or null author does not resolve: the username is null and the
    # client falls back to the raw id (issue #688).
    server = ServerId(uuid.uuid4())
    backup = _backup(server)
    use_case = _FakeUseCase(
        result=[ListedBackup(backup=backup, created_by_username=None)]
    )
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), server.value))
    assert resp.status_code == 200
    assert resp.json()["backups"][0]["created_by_username"] is None


def test_list_unknown_server_is_404() -> None:
    use_case = _FakeUseCase(error=ServerNotFoundError("x"))
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 404


# --- restore ---------------------------------------------------------------


def test_restore_running_is_409() -> None:
    use_case = _FakeUseCase(error=ServerNotStoppedError("x"))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, restore=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/restore"))
    assert resp.status_code == 409
    # A restore refused because the server is running records backup:restore
    # DENIED against the backup.
    assert [e.operation for e in recorder.events] == [ops.BACKUP_RESTORE]
    assert recorder.events[0].outcome is Outcome.DENIED
    assert recorder.events[0].target_type == ops.TARGET_BACKUP


def test_restore_unknown_backup_is_404() -> None:
    use_case = _FakeUseCase(error=BackupNotFoundError("x"))
    app = _app(member=True, allow=True, restore=use_case)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/restore"))
    assert resp.status_code == 404


def test_restore_at_rest_is_204() -> None:
    use_case = _FakeUseCase(result=RestoreResult(forced_corrupt=False, corrupt_count=0))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, restore=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/restore"))
    assert resp.status_code == 204
    # force defaults to False when the query param is absent.
    assert use_case.calls[0]["force"] is False
    # A clean restore records backup:restore SUCCESS against the backup.
    assert [e.operation for e in recorder.events] == [ops.BACKUP_RESTORE]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_BACKUP


def test_restore_corrupt_without_force_is_500_with_reason() -> None:
    # The restore gate (#743) refused a corrupt backup without force: a server-side
    # data fault surfaced as a 500 with a machine-readable reason, matching the
    # create-direction gate (#749).
    use_case = _FakeUseCase(error=BackupCorruptError("x", corrupt_count=3))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, restore=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/restore"))
    assert resp.status_code == 500
    assert resp.json()["reason"] == "working_set_corrupt"
    # The gate-refused restore records backup:restore ERROR against the backup.
    assert [e.operation for e in recorder.events] == [ops.BACKUP_RESTORE]
    assert recorder.events[0].outcome is Outcome.ERROR
    assert recorder.events[0].target_type == ops.TARGET_BACKUP


def test_restore_storage_unavailable_is_503_with_reason() -> None:
    # The store could not serve the archive back (issue #2378): a transient
    # backend fault, 503 storage_unavailable rather than a generic 500.
    use_case = _FakeUseCase(error=BackupStorageUnavailableError("x"))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, restore=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/restore"))
    assert resp.status_code == 503
    assert resp.json()["reason"] == "storage_unavailable"
    assert [e.operation for e in recorder.events] == [ops.BACKUP_RESTORE]
    assert recorder.events[0].outcome is Outcome.ERROR
    assert recorder.events[0].target_type == ops.TARGET_BACKUP


def test_restore_with_force_query_param_passes_force_true() -> None:
    use_case = _FakeUseCase(result=RestoreResult(forced_corrupt=True, corrupt_count=2))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, restore=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/restore?force=true")
    )
    # A forced corrupt restore still publishes -> 204; force was forwarded.
    assert resp.status_code == 204
    assert use_case.calls[0]["force"] is True
    # The deliberate corrupt restore records the distinct backup:force_restore
    # SUCCESS (issue #743), not a routine backup:restore.
    assert [e.operation for e in recorder.events] == [ops.BACKUP_FORCE_RESTORE]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_BACKUP


# --- delete ----------------------------------------------------------------


def test_delete_is_204() -> None:
    use_case = _FakeUseCase()
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, delete=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}"))
    assert resp.status_code == 204
    # A successful delete records backup:delete SUCCESS against the backup.
    assert [e.operation for e in recorder.events] == [ops.BACKUP_DELETE]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_BACKUP


def test_delete_storage_unavailable_is_503_with_reason() -> None:
    # The archive delete drives an object-store delete; an outage there is the same
    # transient backend fault as on create/restore (issue #2378), so one outage
    # produces one status across every backup route.
    use_case = _FakeUseCase(error=BackupStorageUnavailableError("x"))
    app = _app(member=True, allow=True, delete=use_case)
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}"))
    assert resp.status_code == 503
    assert resp.json()["reason"] == "storage_unavailable"


def test_delete_unknown_backup_is_404() -> None:
    use_case = _FakeUseCase(error=BackupNotFoundError("x"))
    app = _app(member=True, allow=True, delete=use_case)
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}"))
    assert resp.status_code == 404


# --- download (issue #281) -------------------------------------------------

_ARCHIVE = b"archive-bytes"


class _FakeDownload:
    """A download use case over fixed archive bytes, ranged like the real one.

    The two methods mirror :class:`DownloadBackup`'s signatures exactly, ids they
    do not need included; the test below holds them there.

    A fresh stream per call (an async generator is exhausted by its first
    consumer, and one test fetches the same archive twice). ``declared``
    overstates the size so a test can model the archive vanishing under an open
    stream (issue #2318); ``mid_stream_error`` raises after the first chunk for
    the other half of that race. ``stream_error`` fails only the open, modelling
    the backup row disappearing between the size read and it.

    ``open_error`` is that same race one step later and is the shape the real
    store has (issue #2415): both backends locate the archive on the stream's
    FIRST iteration, so a delete landing after the size probe fails there, not at
    the ``archive_stream`` call.
    """

    def __init__(
        self,
        data: bytes = _ARCHIVE,
        *,
        chunks: list[bytes] | None = None,
        declared: int | None = None,
        error: Exception | None = None,
        stream_error: Exception | None = None,
        open_error: Exception | None = None,
        mid_stream_error: Exception | None = None,
    ) -> None:
        self._chunks = [data] if chunks is None else chunks
        self._declared = declared
        self._error = error
        self._stream_error = stream_error
        self._open_error = open_error
        self._mid_stream_error = mid_stream_error
        # Every byte_range the edge asked for, so a test can prove the range
        # reached the store rather than being sliced off a full stream (#2372).
        self.ranges: list[tuple[int, int] | None] = []

    async def archive_size(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup_id: BackupId,
    ) -> int:
        if self._error is not None:
            raise self._error
        if self._declared is not None:
            return self._declared
        return sum(len(chunk) for chunk in self._chunks)

    async def archive_stream(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        backup_id: BackupId,
        byte_range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        if self._error is not None:
            raise self._error
        if self._stream_error is not None:
            raise self._stream_error
        self.ranges.append(byte_range)
        return self._stream(byte_range)

    async def _stream(self, byte_range: tuple[int, int] | None) -> AsyncIterator[bytes]:
        if self._open_error is not None:
            raise self._open_error
        if byte_range is None:
            for chunk in self._chunks:
                yield chunk
        else:
            first, last = byte_range
            yield b"".join(self._chunks)[first : last + 1]
        if self._mid_stream_error is not None:
            raise self._mid_stream_error


def test_the_download_double_has_the_real_use_cases_shape() -> None:
    # _FakeDownload is the only stand-in for DownloadBackup in this file, so it is
    # also the only description of that interface a reader gets here. Nothing else
    # keeps the two in step: when #2382 split the use case into archive_size() +
    # archive_stream(), the then-current double kept passing because the tests
    # holding it reject before it is ever called (issue #2384). Comparing the two
    # directly makes the next such split red here, at the double, instead of
    # leaving a double that misdescribes what it stands for.
    def methods(cls: type) -> dict[str, inspect.Signature]:
        return {
            name: inspect.signature(func)
            for name, func in inspect.getmembers(cls, inspect.isfunction)
            if not name.startswith("_")
        }

    assert methods(_FakeDownload) == methods(DownloadBackup)


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {_tokens.issue_access_token(_user.id)}"}


def _download(
    client: TestClient, headers: dict[str, str] | None = None
) -> httpx2.Response:
    return client.get(
        _url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/download"),
        headers=_bearer() if headers is None else headers,
    )


def test_member_without_permission_gets_403_on_download() -> None:
    app = _app(member=True, allow=False, download=_FakeDownload())
    client = next(_client(app))
    assert _download(client).status_code == 403


def test_download_without_credentials_is_401() -> None:
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/download"))
    assert resp.status_code == 401


def test_download_storage_unavailable_is_503_with_reason() -> None:
    # The size probe runs BEFORE any byte is on the wire, so a store outage there
    # can still choose the status (issue #2378): 503 storage_unavailable, not a
    # generic 500. An outage that only strikes mid-stream cannot — the status is
    # already committed — and stays a truncated body.
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=BackupStorageUnavailableError("x")),
    )
    client = next(_client(app))
    resp = _download(client)
    assert resp.status_code == 503
    assert resp.json()["reason"] == "storage_unavailable"


def test_download_streams_archive_with_disposition() -> None:
    app = _app(member=True, allow=True, download=_FakeDownload(b"archive-bytes"))
    client = next(_client(app))
    resp = _download(client)
    assert resp.status_code == 200
    assert resp.content == b"archive-bytes"
    assert resp.headers["content-type"] == "application/gzip"
    assert "attachment" in resp.headers["content-disposition"]
    assert ".tar.gz" in resp.headers["content-disposition"]


def test_download_declares_content_length_matching_streamed_bytes() -> None:
    # The load-bearing invariant (issue #2312): a declared length that disagrees
    # with the streamed byte count corrupts or hangs the response over HTTP/2.
    chunks = [b"first-chunk", b"second-chunk", b"third"]
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(chunks=chunks),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = _download(client)
    assert resp.status_code == 200
    assert resp.content == b"".join(chunks)
    assert int(resp.headers["content-length"]) == len(resp.content)
    assert resp.headers["cache-control"] == "no-store"
    # The download is still audited exactly once.
    assert [e.operation for e in recorder.events] == [ops.BACKUP_DOWNLOAD]
    assert recorder.events[0].outcome is Outcome.SUCCESS


def test_download_aborts_when_stream_ends_short_of_declared_length() -> None:
    # A concurrent DeleteBackup (or the retention prune) can remove the archive
    # under an open stream (issue #2318), leaving the body short of the declared
    # Content-Length. A real server (uvicorn + h11) already rejects that at the
    # wire; counting the bytes names the mismatch here instead, so the invariant
    # holds without depending on the ASGI server to notice.
    download = _FakeDownload(chunks=[b"first-chunk", b"second-chunk"], declared=1024)
    app = _app(member=True, allow=True, download=download)
    client = next(_client(app))
    with pytest.raises(ShortResponseBodyError):
        _download(client)


def test_download_aborts_when_archive_disappears_mid_stream() -> None:
    # The other half of the same race (issue #2318): the archive is removed while
    # the stream is open and the next read raises, which the store seam translates
    # to BackupNotFoundError. Headers are already on the wire, so there is no 404
    # to send — the response must abort rather than end quietly short.
    download = _FakeDownload(
        chunks=[b"first-chunk"],
        declared=1024,
        mid_stream_error=BackupNotFoundError("x"),
    )
    app = _app(member=True, allow=True, download=download)
    client = next(_client(app))
    with pytest.raises(BackupNotFoundError):
        _download(client)


def test_download_unknown_backup_is_404() -> None:
    app = _app(
        member=True, allow=True, download=_FakeDownload(error=BackupNotFoundError("x"))
    )
    client = next(_client(app))
    assert _download(client).status_code == 404


@pytest.mark.parametrize("error", [BackupNotFoundError("x"), ServerNotFoundError("x")])
def test_download_of_a_backup_deleted_before_the_open_is_404(error: Exception) -> None:
    # The size is read before the stream is opened, so a delete can land between
    # the two. Nothing is on the wire yet, so it is still a plain 404.
    app = _app(member=True, allow=True, download=_FakeDownload(stream_error=error))
    client = next(_client(app))
    assert _download(client).status_code == 404


@pytest.mark.parametrize("header", [None, "bytes=0-4"])
def test_download_of_an_archive_deleted_before_the_first_read_is_404(
    header: str | None,
) -> None:
    # The same race one step later, and the shape the real store actually has
    # (issue #2415): both backends locate the archive on the stream's FIRST
    # iteration, so a delete landing after the size probe fails there. Starlette
    # writes the status and Content-Length before it touches the body iterator, so
    # that failure used to arrive as a 200 declaring a length the body could never
    # deliver. The route begins the stream first, which puts the miss back where a
    # status can still be chosen.
    #
    # The ranged path is the same window with a length derived from the same probe
    # (issue #2382), so it is answered the same way.
    recorder = RecordingAuditRecorder()
    download = _FakeDownload(open_error=BackupNotFoundError("x"), declared=1024)
    app = _app(member=True, allow=True, download=download, recorder=recorder)
    client = next(_client(app))
    headers = _bearer() if header is None else {**_bearer(), "Range": header}
    resp = _download(client, headers)
    assert resp.status_code == 404
    # It is the miss, not a partial: no representation was described...
    assert "content-range" not in resp.headers
    assert resp.headers["content-type"] != "application/gzip"
    # ... and nothing was recorded, exactly as for the 404s above.
    assert recorder.events == []


def test_download_storage_outage_at_the_open_is_503() -> None:
    # The open reaches the store too, and now runs before any byte is on the wire
    # (issue #2415), so an outage that begins after the size probe can still
    # choose the retryable status the probe's own outage answers with (#2378).
    download = _FakeDownload(open_error=BackupStorageUnavailableError("x"))
    app = _app(member=True, allow=True, download=download)
    client = next(_client(app))
    resp = _download(client)
    assert resp.status_code == 503
    assert resp.json()["reason"] == "storage_unavailable"


# --- resumable download: Range (issue #2372) -------------------------------

# 26 bytes, so a range's boundaries are readable in a failure message.
_RANGED = b"abcdefghijklmnopqrstuvwxyz"


def test_full_download_advertises_range_support_and_an_etag() -> None:
    # Without Accept-Ranges a browser will not even attempt a ranged resume, and
    # without an ETag it cannot validate that the bytes it resumes into are the
    # same representation.
    app = _app(member=True, allow=True, download=_FakeDownload(_RANGED))
    client = next(_client(app))
    resp = _download(client)
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["etag"].startswith('"')


def test_the_etag_covers_the_archive_size() -> None:
    # The archive is immutable per backup id, so the id plus its byte count
    # identifies the exact bytes: two archives of different length under the
    # same id could never share a validator.
    app = _app(member=True, allow=True, download=_FakeDownload(_RANGED))
    client = next(_client(app))
    path = _url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/download")
    first = client.get(path, headers=_bearer())
    same = client.get(path, headers=_bearer())
    assert first.headers["etag"] == same.headers["etag"]

    app = _app(member=True, allow=True, download=_FakeDownload(_RANGED + b"!"))
    other = next(_client(app)).get(path, headers=_bearer())
    assert other.headers["etag"] != first.headers["etag"]


def test_open_ended_range_resumes_from_the_offset() -> None:
    download = _FakeDownload(_RANGED)
    app = _app(member=True, allow=True, download=download)
    client = next(_client(app))
    resp = _download(client, {**_bearer(), "Range": "bytes=20-"})
    assert resp.status_code == 206
    assert resp.content == _RANGED[20:]
    assert resp.headers["content-range"] == "bytes 20-25/26"
    assert int(resp.headers["content-length"]) == 6
    assert resp.headers["accept-ranges"] == "bytes"
    # The store was asked for exactly those bytes, not the whole archive.
    assert download.ranges == [(20, 25)]


def test_closed_range_serves_exactly_that_span() -> None:
    app = _app(member=True, allow=True, download=_FakeDownload(_RANGED))
    client = next(_client(app))
    resp = _download(client, {**_bearer(), "Range": "bytes=5-9"})
    assert resp.status_code == 206
    assert resp.content == b"fghij"
    assert resp.headers["content-range"] == "bytes 5-9/26"
    assert int(resp.headers["content-length"]) == 5


def test_suffix_range_serves_the_final_bytes() -> None:
    app = _app(member=True, allow=True, download=_FakeDownload(_RANGED))
    client = next(_client(app))
    resp = _download(client, {**_bearer(), "Range": "bytes=-3"})
    assert resp.status_code == 206
    assert resp.content == b"xyz"
    assert resp.headers["content-range"] == "bytes 23-25/26"


@pytest.mark.parametrize(
    ("header", "expected", "content_range"),
    [
        ("bytes=0-0", b"a", "bytes 0-0/26"),
        ("bytes=25-25", b"z", "bytes 25-25/26"),
        ("bytes=0-25", _RANGED, "bytes 0-25/26"),
        ("bytes=1-25", _RANGED[1:], "bytes 1-25/26"),
        ("bytes=0-99", _RANGED, "bytes 0-25/26"),
    ],
)
def test_range_boundaries(header: str, expected: bytes, content_range: str) -> None:
    app = _app(member=True, allow=True, download=_FakeDownload(_RANGED))
    client = next(_client(app))
    resp = _download(client, {**_bearer(), "Range": header})
    assert resp.status_code == 206
    assert resp.content == expected
    assert resp.headers["content-range"] == content_range
    assert int(resp.headers["content-length"]) == len(expected)


def test_unsatisfiable_range_is_416_naming_the_size() -> None:
    recorder = RecordingAuditRecorder()
    download = _FakeDownload(_RANGED)
    app = _app(member=True, allow=True, download=download, recorder=recorder)
    client = next(_client(app))
    resp = _download(client, {**_bearer(), "Range": "bytes=26-"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */26"
    assert resp.json()["reason"] == "range_not_satisfiable"
    # No stream is opened for a range that cannot be served...
    assert download.ranges == []
    # ... so nothing is recorded either — as for a 404.
    assert recorder.events == []


@pytest.mark.parametrize(
    "header",
    ["bytes=abc", "items=0-25", "bytes=10-5", "bytes=0-4,10-14"],
)
def test_unusable_range_serves_the_whole_archive(header: str) -> None:
    # A malformed or multi-range request is answered as if Range were absent.
    download = _FakeDownload(_RANGED)
    app = _app(member=True, allow=True, download=download)
    client = next(_client(app))
    resp = _download(client, {**_bearer(), "Range": header})
    assert resp.status_code == 200
    assert resp.content == _RANGED
    assert download.ranges == [None]


def test_partial_download_is_audited_like_a_full_one() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True, allow=True, download=_FakeDownload(_RANGED), recorder=recorder
    )
    client = next(_client(app))
    resp = _download(client, {**_bearer(), "Range": "bytes=10-"})
    assert resp.status_code == 206
    assert [e.operation for e in recorder.events] == [ops.BACKUP_DOWNLOAD]
    assert recorder.events[0].outcome is Outcome.SUCCESS


def test_partial_download_aborts_when_the_range_ends_short() -> None:
    # The declared length of a 206 is the range's length, and the same
    # short-body guard applies to it (issues #2312/#2318): the store returning
    # fewer bytes than the range promised fails the response here.
    download = _FakeDownload(chunks=[b"short"], declared=1024)
    app = _app(member=True, allow=True, download=download)
    client = next(_client(app))
    with pytest.raises(ShortResponseBodyError):
        _download(client, {**_bearer(), "Range": "bytes=0-99"})


def test_if_range_matching_the_etag_applies_the_range() -> None:
    app = _app(member=True, allow=True, download=_FakeDownload(_RANGED))
    client = next(_client(app))
    path = _url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/download")
    etag = client.get(path, headers=_bearer()).headers["etag"]

    resp = client.get(
        path, headers={**_bearer(), "Range": "bytes=20-", "If-Range": etag}
    )
    assert resp.status_code == 206
    assert resp.content == _RANGED[20:]


def test_if_range_not_matching_serves_the_whole_archive() -> None:
    # The representation the client started from is gone, so resuming into it
    # would splice two different archives: send the current one whole instead.
    app = _app(member=True, allow=True, download=_FakeDownload(_RANGED))
    client = next(_client(app))
    resp = _download(
        client, headers={**_bearer(), "Range": "bytes=20-", "If-Range": '"stale"'}
    )
    assert resp.status_code == 200
    assert resp.content == _RANGED


def test_if_range_carrying_the_weak_form_of_our_etag_serves_the_whole_archive() -> None:
    # If-Range is compared with the STRONG function (RFC 9110 Section 13.1.5), so
    # ``W/`` in front of our own tag is deliberately not a match.
    app = _app(member=True, allow=True, download=_FakeDownload(_RANGED))
    client = next(_client(app))
    path = _url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/download")
    etag = client.get(path, headers=_bearer()).headers["etag"]

    resp = client.get(
        path, headers={**_bearer(), "Range": "bytes=20-", "If-Range": f"W/{etag}"}
    )
    assert resp.status_code == 200
    assert resp.content == _RANGED


def test_if_range_without_a_range_is_a_plain_full_download() -> None:
    # If-Range only ever gates a Range; on its own it decides nothing, whether or
    # not it matches.
    download = _FakeDownload(_RANGED)
    app = _app(member=True, allow=True, download=download)
    client = next(_client(app))
    path = _url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/download")
    etag = client.get(path, headers=_bearer()).headers["etag"]

    matching = client.get(path, headers={**_bearer(), "If-Range": etag})
    stale = client.get(path, headers={**_bearer(), "If-Range": '"stale"'})

    assert matching.status_code == 200
    assert matching.content == _RANGED
    assert stale.status_code == 200
    assert stale.content == _RANGED
    assert download.ranges == [None, None, None]


# --- download grants (issue #2313) -----------------------------------------


def _grant_url(
    community: uuid.UUID, server: uuid.UUID, backup: uuid.UUID, *, subject: User
) -> str:
    """The download URL a grant minted for this triple would produce."""

    issued = _tokens.issue_download_grant(
        subject.id, download_grant_resource(community, server, backup)
    )
    return _url(community, server, f"/{backup}/download?grant={issued.token}")


def _mint(
    client: TestClient, community: uuid.UUID, server: uuid.UUID, backup: uuid.UUID
) -> httpx2.Response:
    return client.post(
        _url(community, server, f"/{backup}/download-grant"), headers=_bearer()
    )


def test_non_member_gets_404_on_download_grant() -> None:
    app = _app(member=False, allow=True, resolve=_FakeUseCase())
    client = next(_client(app))
    resp = _mint(client, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_download_grant() -> None:
    app = _app(member=True, allow=False, resolve=_FakeUseCase())
    client = next(_client(app))
    resp = _mint(client, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert resp.status_code == 403


def test_download_grant_for_unknown_backup_is_404() -> None:
    app = _app(
        member=True, allow=True, resolve=_FakeUseCase(error=BackupNotFoundError("x"))
    )
    client = next(_client(app))
    resp = _mint(client, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    assert resp.status_code == 404


def test_download_grant_response_is_not_cached_and_reports_expiry() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, resolve=_FakeUseCase())
    client = next(_client(app))
    resp = _mint(client, community, server, backup)
    assert resp.status_code == 200
    # A URL that carries a credential must never sit in a shared cache.
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["download_url"].startswith(
        f"/api/communities/{community}/servers/{server}/backups/{backup}/download?grant="
    )
    assert body["expires_at"] == (
        _NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS)
    ).isoformat().replace("+00:00", "Z")


def test_minting_a_grant_records_no_audit_event() -> None:
    # Bytes leave the system at redemption, not at issuance (issue #2313).
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, resolve=_FakeUseCase(), recorder=recorder)
    client = next(_client(app))
    assert _mint(client, uuid.uuid4(), uuid.uuid4(), uuid.uuid4()).status_code == 200
    assert recorder.events == []


def test_minted_url_downloads_without_an_authorization_header() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    download = _FakeDownload(_ARCHIVE)
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        resolve=_FakeUseCase(),
        download=download,
        recorder=recorder,
    )
    client = next(_client(app))
    url = _mint(client, community, server, backup).json()["download_url"]

    resp = client.get(url)

    assert resp.status_code == 200
    assert resp.content == _ARCHIVE
    assert resp.headers["content-type"] == "application/gzip"
    assert int(resp.headers["content-length"]) == len(_ARCHIVE)
    assert resp.headers["cache-control"] == "no-store"
    assert ".tar.gz" in resp.headers["content-disposition"]
    # Exactly one audit row, at redemption, with the grant's subject as actor.
    assert [e.operation for e in recorder.events] == [ops.BACKUP_DOWNLOAD]
    assert recorder.events[0].actor_id == _user.id.value


def test_grant_redeemed_download_matches_the_bearer_response() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        resolve=_FakeUseCase(),
        download=_FakeDownload(_ARCHIVE),
    )
    client = next(_client(app))
    path = _url(community, server, f"/{backup}/download")

    with_bearer = client.get(path, headers=_bearer())
    with_grant = client.get(_grant_url(community, server, backup, subject=_user))

    assert with_grant.status_code == with_bearer.status_code == 200
    assert with_grant.content == with_bearer.content
    for header in ("content-type", "content-length", "content-disposition"):
        assert with_grant.headers[header] == with_bearer.headers[header]
    assert with_grant.headers["cache-control"] == with_bearer.headers["cache-control"]


def test_grant_is_rejected_after_its_ttl() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    url = _grant_url(community, server, backup, subject=_user)

    _clock.set(_NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS))

    assert client.get(url).status_code == 401


def test_grant_is_rejected_on_another_backup() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    issued = _tokens.issue_download_grant(
        _user.id, download_grant_resource(community, server, uuid.uuid4())
    )
    other = uuid.uuid4()

    resp = client.get(
        _url(community, server, f"/{other}/download?grant={issued.token}")
    )

    assert resp.status_code == 401


def test_grant_is_rejected_under_another_server_or_community() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    issued = _tokens.issue_download_grant(
        _user.id, download_grant_resource(community, server, backup)
    )

    other_server = client.get(
        _url(community, uuid.uuid4(), f"/{backup}/download?grant={issued.token}")
    )
    other_community = client.get(
        _url(uuid.uuid4(), server, f"/{backup}/download?grant={issued.token}")
    )

    assert other_server.status_code == 401
    assert other_community.status_code == 401


def test_grant_is_not_accepted_as_a_bearer_token() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    issued = _tokens.issue_download_grant(
        _user.id, download_grant_resource(community, server, backup)
    )

    resp = client.get(
        _url(community, server, f"/{backup}/download"),
        headers={"Authorization": f"Bearer {issued.token}"},
    )

    assert resp.status_code == 401


def test_access_token_is_not_accepted_as_a_grant() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    access = _tokens.issue_access_token(_user.id)

    resp = client.get(_url(community, server, f"/{backup}/download?grant={access}"))

    assert resp.status_code == 401


def test_grant_loses_to_a_permission_revoked_after_issuance() -> None:
    # The grant proves identity, never authority: authorization is decided afresh.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=False, download=_FakeDownload())
    client = next(_client(app))

    resp = client.get(_grant_url(community, server, backup, subject=_user))

    assert resp.status_code == 403


def test_grant_loses_to_a_membership_removed_after_issuance() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=False, allow=True, download=_FakeDownload())
    client = next(_client(app))

    resp = client.get(_grant_url(community, server, backup, subject=_user))

    assert resp.status_code == 404


def test_grant_for_a_deactivated_subject_is_rejected() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        subject=make_user(active=False),
        download=_FakeDownload(),
    )
    client = next(_client(app))

    resp = client.get(_grant_url(community, server, backup, subject=_user))

    assert resp.status_code == 401


# --- the download cookie (issue #2373) -------------------------------------
#
# A grant lives 30 s but a multi-GB transfer does not, so the browser's retry of
# an interrupted download re-presented an expired grant and got a 401. Redeeming
# a grant now also mints an httpOnly cookie carrying the same resource-scoped
# authority for longer, and the retry authenticates with that.


def _set_cookie_header(resp: httpx2.Response) -> str:
    # The TestClient runs over plain HTTP, so a Secure cookie is never stored in
    # the jar; inspect the raw Set-Cookie header instead.
    headers: list[str] = resp.headers.get_list("set-cookie")
    for header in headers:
        if header.startswith(f"{DOWNLOAD_COOKIE_NAME}="):
            return header
    raise AssertionError(f"no download cookie in {headers!r}")


def _assert_no_download_cookie(resp: httpx2.Response) -> None:
    headers: list[str] = resp.headers.get_list("set-cookie")
    assert not any(h.startswith(f"{DOWNLOAD_COOKIE_NAME}=") for h in headers), (
        f"unexpected download cookie in {headers!r}"
    )


def _cookie_header(
    community: uuid.UUID,
    server: uuid.UUID,
    backup: uuid.UUID,
    *,
    subject: User | None = None,
) -> dict[str, str]:
    """The Cookie header a browser holding this triple's download cookie sends."""

    value = _tokens.issue_download_cookie(
        (subject if subject is not None else _user).id,
        download_grant_resource(community, server, backup),
    )
    return {"Cookie": f"{DOWNLOAD_COOKIE_NAME}={value}"}


def test_grant_redemption_sets_a_path_scoped_httponly_cookie() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(_ARCHIVE))
    client = next(_client(app))

    resp = client.get(_grant_url(community, server, backup, subject=_user))

    assert resp.status_code == 200
    cookie = _set_cookie_header(resp)
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    # Starlette renders the SameSite value lowercase.
    assert "SameSite=strict" in cookie
    # Scoped to this one download's own URL path: the browser attaches it to no
    # other route, this server's other backups included.
    assert (
        f"Path=/api/communities/{community}/servers/{server}"
        f"/backups/{backup}/download" in cookie
    )
    assert f"Max-Age={_COOKIE_TTL_SECONDS}" in cookie


def test_a_bearer_download_sets_no_cookie() -> None:
    # A non-browser client never asked for one (the posture issue #372 set for the
    # refresh cookie), and it already carries a credential that outlives the grant.
    app = _app(member=True, allow=True, download=_FakeDownload(_ARCHIVE))
    client = next(_client(app))

    resp = _download(client)

    assert resp.status_code == 200
    _assert_no_download_cookie(resp)


def test_expired_grant_download_is_retried_with_the_cookie() -> None:
    # The whole point: the URL the browser retries still carries the grant it was
    # given, which has expired by then.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(_ARCHIVE))
    client = next(_client(app))
    url = _grant_url(community, server, backup, subject=_user)
    cookie = _cookie_header(community, server, backup)

    _clock.set(_NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS))

    assert client.get(url).status_code == 401
    resumed = client.get(url, headers=cookie)
    assert resumed.status_code == 200
    assert resumed.content == _ARCHIVE


def test_expired_grant_ranged_retry_is_served_a_206_with_the_cookie() -> None:
    # The retry a resume actually issues (issue #2372): a Range request, which the
    # cookie has to authenticate exactly like a full one.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(_ARCHIVE))
    client = next(_client(app))
    url = _grant_url(community, server, backup, subject=_user)
    headers = _cookie_header(community, server, backup) | {"Range": "bytes=5-"}

    _clock.set(_NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS))
    resp = client.get(url, headers=headers)

    assert resp.status_code == 206
    assert resp.content == _ARCHIVE[5:]
    assert (
        resp.headers["content-range"] == f"bytes 5-{len(_ARCHIVE) - 1}/{len(_ARCHIVE)}"
    )


def test_cookie_alone_downloads_without_any_grant_in_the_url() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(_ARCHIVE))
    client = next(_client(app))

    resp = client.get(
        _url(community, server, f"/{backup}/download"),
        headers=_cookie_header(community, server, backup),
    )

    assert resp.status_code == 200
    assert resp.content == _ARCHIVE


def test_cookie_is_rejected_after_its_own_ttl() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    headers = _cookie_header(community, server, backup)

    _clock.set(_NOW + dt.timedelta(seconds=_COOKIE_TTL_SECONDS))

    resp = client.get(_url(community, server, f"/{backup}/download"), headers=headers)
    assert resp.status_code == 401


def test_cookie_is_rejected_on_another_backup() -> None:
    # The Path scope narrows what the browser sends; the signed resource claim is
    # what decides. A cookie replayed onto a sibling resource opens nothing.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    headers = _cookie_header(community, server, uuid.uuid4())

    resp = client.get(
        _url(community, server, f"/{uuid.uuid4()}/download"), headers=headers
    )

    assert resp.status_code == 401


def test_cookie_is_rejected_under_another_server_or_community() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    headers = _cookie_header(community, server, backup)

    other_server = client.get(
        _url(community, uuid.uuid4(), f"/{backup}/download"), headers=headers
    )
    other_community = client.get(
        _url(uuid.uuid4(), server, f"/{backup}/download"), headers=headers
    )

    assert other_server.status_code == 401
    assert other_community.status_code == 401


def test_cookie_is_not_accepted_in_the_query_string() -> None:
    # The separation that keeps the longer-lived credential out of access logs,
    # browser history and any Referer.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    value = _tokens.issue_download_cookie(
        _user.id, download_grant_resource(community, server, backup)
    )

    resp = client.get(_url(community, server, f"/{backup}/download?grant={value}"))

    assert resp.status_code == 401


def test_grant_is_not_accepted_as_the_cookie() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    issued = _tokens.issue_download_grant(
        _user.id, download_grant_resource(community, server, backup)
    )

    resp = client.get(
        _url(community, server, f"/{backup}/download"),
        headers={"Cookie": f"{DOWNLOAD_COOKIE_NAME}={issued.token}"},
    )

    assert resp.status_code == 401


def test_cookie_is_not_accepted_as_a_bearer_token() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    value = _tokens.issue_download_cookie(
        _user.id, download_grant_resource(community, server, backup)
    )

    resp = client.get(
        _url(community, server, f"/{backup}/download"),
        headers={"Authorization": f"Bearer {value}"},
    )

    assert resp.status_code == 401


def test_cookie_does_not_rescue_a_rejected_bearer_token() -> None:
    # The header still wins outright: the cookie is a fallback transport for a
    # browser, never a way around a session token the server refused.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    headers = _cookie_header(community, server, backup) | {
        "Authorization": "Bearer not-a-token"
    }

    resp = client.get(_url(community, server, f"/{backup}/download"), headers=headers)

    assert resp.status_code == 401


def test_a_cookie_authenticated_download_does_not_renew_the_cookie() -> None:
    # No sliding window: a cookie's life is bounded by the redemption that minted
    # it, so repeated retries cannot extend it indefinitely.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(_ARCHIVE))
    client = next(_client(app))

    resp = client.get(
        _url(community, server, f"/{backup}/download"),
        headers=_cookie_header(community, server, backup),
    )

    assert resp.status_code == 200
    _assert_no_download_cookie(resp)


def test_a_cookie_authenticated_download_acts_as_the_cookies_subject() -> None:
    # The cookie is the identity: the bytes leave attributed to whoever it names,
    # not to whatever subject some other channel would have resolved.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True, allow=True, download=_FakeDownload(_ARCHIVE), recorder=recorder
    )
    client = next(_client(app))

    resp = client.get(
        _url(community, server, f"/{backup}/download"),
        headers=_cookie_header(community, server, backup),
    )

    assert resp.status_code == 200
    assert [e.operation for e in recorder.events] == [ops.BACKUP_DOWNLOAD]
    assert recorder.events[0].actor_id == _user.id.value


def test_a_cookie_naming_an_unknown_user_is_rejected() -> None:
    # Nothing but the signature vouches for the ``sub`` claim, so the row is loaded:
    # a cookie for a user this deployment does not have opens nothing.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))

    resp = client.get(
        _url(community, server, f"/{backup}/download"),
        headers=_cookie_header(
            community,
            server,
            backup,
            subject=make_user(),  # never seeded
        ),
    )

    assert resp.status_code == 401


def test_cookie_loses_to_a_permission_revoked_after_the_mint() -> None:
    # Identity, never authority — the same posture the grant has.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=False, download=_FakeDownload())
    client = next(_client(app))

    resp = client.get(
        _url(community, server, f"/{backup}/download"),
        headers=_cookie_header(community, server, backup),
    )

    assert resp.status_code == 403


def test_cookie_for_a_deactivated_subject_is_rejected() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        subject=make_user(active=False),
        download=_FakeDownload(),
    )
    client = next(_client(app))

    resp = client.get(
        _url(community, server, f"/{backup}/download"),
        headers=_cookie_header(community, server, backup),
    )

    assert resp.status_code == 401


def test_a_grant_redemption_that_fails_the_gate_mints_no_cookie() -> None:
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=False, download=_FakeDownload())
    client = next(_client(app))

    resp = client.get(_grant_url(community, server, backup, subject=_user))

    assert resp.status_code == 403
    _assert_no_download_cookie(resp)


def test_a_grant_redemption_that_404s_mints_no_cookie() -> None:
    # The gate passed but the archive is gone: no credential for bytes never served.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=BackupNotFoundError("x")),
    )
    client = next(_client(app))

    resp = client.get(_grant_url(community, server, backup, subject=_user))

    assert resp.status_code == 404
    _assert_no_download_cookie(resp)


def test_the_browser_jar_carries_the_cookie_back_to_the_retry() -> None:
    """End to end through a real cookie jar, over https so Secure applies.

    The attribute assertions above cannot show that the Path and Secure scoping
    actually let a client resend the cookie — a mistake in either fails silently,
    as a download that simply stops resuming.
    """

    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(_ARCHIVE))
    url = _grant_url(community, server, backup, subject=_user)
    with TestClient(app, base_url="https://testserver") as client:  # type: ignore[arg-type]
        assert client.get(url).status_code == 200
        # The grant in the URL has expired; only the jar's cookie is left.
        _clock.set(_NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS))
        assert client.get(url).status_code == 200
        # ...and it is scoped to this download alone.
        other = _url(community, server, f"/{uuid.uuid4()}/download")
        assert client.get(other).status_code == 401


def test_a_download_with_no_credential_at_all_is_401_before_the_path_is_parsed() -> (
    None
):
    # Reading the cookie must not pull the resource composition ahead of the
    # no-credential 401: composing it parses the path ids, which would turn this
    # into a 422 (issue #630's re-raise) instead of the 401 it has always been.
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))

    resp = client.get(
        f"/api/communities/notauuid/servers/{uuid.uuid4()}"
        f"/backups/{uuid.uuid4()}/download"
    )

    assert resp.status_code == 401


def test_cookie_omits_secure_when_configured_for_plain_http() -> None:
    # The localhost-dev knob: with Secure set the browser stores nothing over
    # plain HTTP and every resume silently reverts to the 401 this fixes.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(_ARCHIVE))
    settings = _shared_app.state.settings
    insecure = settings.model_copy(
        update={
            "auth": settings.auth.model_copy(
                update={
                    "token": settings.auth.token.model_copy(
                        update={"download_cookie_secure": False}
                    )
                }
            )
        }
    )
    _shared_app.dependency_overrides[get_settings] = lambda: insecure
    client = next(_client(app))

    resp = client.get(_grant_url(community, server, backup, subject=_user))

    assert resp.status_code == 200
    cookie = _set_cookie_header(resp)
    assert "Secure" not in cookie
    assert "HttpOnly" in cookie


# --- Cache-Control on the served archive (issue #2491) ---------------------


def test_backup_download_declares_no_store_under_every_credential() -> None:
    # The header belongs to the response being a per-user body, not to the
    # credential that fetched it, and a partial response carries the same bytes
    # as the whole one. A cookie-authenticated request in particular carries no
    # Authorization, so RFC 9111 Section 3.5's default protection from shared
    # caches does not cover it.
    community, server, backup = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload())
    client = next(_client(app))
    url = _url(community, server, f"/{backup}/download")
    cookie = _cookie_header(community, server, backup)
    # Last, because redeeming a grant mints the cookie into the client's jar.
    grant_url = _grant_url(community, server, backup, subject=_user)

    responses = [
        client.get(url, headers=_bearer()),
        client.get(url, headers={**_bearer(), "Range": "bytes=0-3"}),
        client.get(url, headers=cookie),
        client.get(url, headers={**cookie, "Range": "bytes=0-3"}),
        client.get(grant_url),
        client.get(grant_url, headers={"Range": "bytes=0-3"}),
    ]

    assert [r.status_code for r in responses] == [200, 206, 200, 206, 200, 206]
    for resp in responses:
        assert resp.headers["cache-control"] == "no-store"


# --- upload (issue #281) ---------------------------------------------------


def _multipart() -> dict[str, tuple[str, bytes, str]]:
    return {"file": ("backup.tar.gz", b"\x1f\x8bcontent", "application/gzip")}


def test_member_without_permission_gets_403_on_upload() -> None:
    app = _app(member=True, allow=False, upload=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/upload"), files=_multipart())
    assert resp.status_code == 403


def test_upload_returns_201_and_passes_actor() -> None:
    server = ServerId(uuid.uuid4())
    use_case = _FakeUseCase(
        result=Backup(
            id=BackupId(uuid.uuid4()),
            server_id=server,
            storage_ref="ref",
            size_bytes=9,
            source=BackupSource.UPLOADED,
            health=BackupHealth.UNKNOWN,
            created_by=uuid.uuid4(),
            created_at=_NOW,
        )
    )
    app = _app(member=True, allow=True, upload=use_case)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), server.value, "/upload"), files=_multipart())
    assert resp.status_code == 201
    assert resp.json()["source"] == "uploaded"
    assert resp.json()["health"] == "unknown"
    assert isinstance(use_case.calls[0]["created_by"], uuid.UUID)
    assert use_case.calls[0]["content"] == b"\x1f\x8bcontent"


def test_upload_invalid_archive_is_422() -> None:
    use_case = _FakeUseCase(error=InvalidBackupArchiveError("bad"))
    app = _app(member=True, allow=True, upload=use_case)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/upload"), files=_multipart())
    assert resp.status_code == 422


def test_upload_too_large_is_413() -> None:
    use_case = _FakeUseCase(error=FileTooLargeError("big"))
    app = _app(member=True, allow=True, upload=use_case)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/upload"), files=_multipart())
    assert resp.status_code == 413


def test_upload_storage_unavailable_is_503_with_reason() -> None:
    # The validated archive could not be written to the store (issue #2378):
    # 503 storage_unavailable, so the client knows to retry the upload.
    use_case = _FakeUseCase(error=BackupStorageUnavailableError("x"))
    app = _app(member=True, allow=True, upload=use_case)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/upload"), files=_multipart())
    assert resp.status_code == 503
    assert resp.json()["reason"] == "storage_unavailable"


def test_upload_unknown_server_is_404() -> None:
    use_case = _FakeUseCase(error=ServerNotFoundError("x"))
    app = _app(member=True, allow=True, upload=use_case)
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/upload"), files=_multipart())
    assert resp.status_code == 404


# --- per-server statistics (issue #281) ------------------------------------


def test_member_without_permission_gets_403_on_statistics() -> None:
    app = _app(member=True, allow=False, statistics=_FakeUseCase())
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/statistics"))
    assert resp.status_code == 403


def test_statistics_returns_aggregate() -> None:
    use_case = _FakeUseCase(result=_stats())
    app = _app(member=True, allow=True, statistics=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/statistics"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["total_bytes"] == 30
    assert body["unknown_size_count"] == 1
    # Canonical RFC 3339 UTC form: the ``Z`` suffix, not ``+00:00`` (issue #632).
    assert body["newest"] == "2026-06-04T12:00:00Z"


def test_statistics_unknown_server_is_404() -> None:
    use_case = _FakeUseCase(error=ServerNotFoundError("x"))
    app = _app(member=True, allow=True, statistics=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/statistics"))
    assert resp.status_code == 404


# --- global statistics (platform-admin, issue #281) ------------------------


def test_global_statistics_requires_platform_admin() -> None:
    use_case = _FakeUseCase(result=_stats())
    app = _app(member=True, allow=True, global_statistics=use_case, is_admin=False)
    client = next(_client(app))
    resp = client.get("/api/backups/statistics")
    assert resp.status_code == 403


def test_global_statistics_admin_returns_aggregate() -> None:
    use_case = _FakeUseCase(result=_stats())
    app = _app(member=True, allow=True, global_statistics=use_case, is_admin=True)
    client = next(_client(app))
    resp = client.get("/api/backups/statistics")
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


# --- retention policy (issue #1841) -----------------------------------------


def test_put_retention_non_member_is_404() -> None:
    app = _app(member=False, allow=True, set_retention=_FakeUseCase())
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4(), "/retention"), json={"keep_last": 3}
    )
    assert resp.status_code == 404


def test_put_retention_without_permission_is_403() -> None:
    app = _app(member=True, allow=False, set_retention=_FakeUseCase())
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4(), "/retention"), json={"keep_last": 3}
    )
    assert resp.status_code == 403


def test_put_retention_returns_the_saved_policy() -> None:
    use_case = _FakeUseCase(result=RetentionPolicy.from_fields(keep_last=3))
    app = _app(member=True, allow=True, set_retention=use_case)
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4(), "/retention"), json={"keep_last": 3}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["keep_last"] == 3
    assert (body["daily"], body["weekly"], body["monthly"]) == (None, None, None)
    # The raw fields are forwarded to the use case (validation lives there).
    assert use_case.calls[0]["keep_last"] == 3


def test_put_retention_tiered_returns_the_saved_policy() -> None:
    use_case = _FakeUseCase(
        result=RetentionPolicy.from_fields(daily=7, weekly=4, monthly=6)
    )
    app = _app(member=True, allow=True, set_retention=use_case)
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4(), "/retention"),
        json={"daily": 7, "weekly": 4, "monthly": 6},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["keep_last"] is None
    assert (body["daily"], body["weekly"], body["monthly"]) == (7, 4, 6)


def test_put_retention_records_set_retention_audit() -> None:
    # A retention policy write is a privileged mutation: it is audited with the
    # acting user and targets the server, so the causal actor behind the
    # actor-None backup:delete prune rows stays recoverable (issue #1841).
    server_id = uuid.uuid4()
    community_id = uuid.uuid4()
    use_case = _FakeUseCase(result=RetentionPolicy.from_fields(keep_last=3))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, set_retention=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.put(
        _url(community_id, server_id, "/retention"), json={"keep_last": 3}
    )
    assert resp.status_code == 200
    assert [e.operation for e in recorder.events] == [ops.BACKUP_SET_RETENTION]
    event = recorder.events[0]
    assert event.outcome is Outcome.SUCCESS
    assert isinstance(event.actor_id, uuid.UUID)
    assert event.community_id == community_id
    assert event.target_type == ops.TARGET_SERVER
    assert event.target_id == server_id


def test_put_retention_failure_is_not_audited() -> None:
    use_case = _FakeUseCase(error=InvalidRetentionPolicyError("x"))
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, set_retention=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4(), "/retention"), json={"keep_last": 0}
    )
    assert resp.status_code == 422
    assert recorder.events == []


def test_delete_retention_records_clear_retention_audit() -> None:
    server_id = uuid.uuid4()
    community_id = uuid.uuid4()
    use_case = _FakeUseCase()
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, clear_retention=use_case, recorder=recorder)
    client = next(_client(app))
    resp = client.delete(_url(community_id, server_id, "/retention"))
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.BACKUP_CLEAR_RETENTION]
    event = recorder.events[0]
    assert event.outcome is Outcome.SUCCESS
    assert isinstance(event.actor_id, uuid.UUID)
    assert event.community_id == community_id
    assert event.target_type == ops.TARGET_SERVER
    assert event.target_id == server_id


def test_put_retention_invalid_policy_is_422() -> None:
    use_case = _FakeUseCase(error=InvalidRetentionPolicyError("x"))
    app = _app(member=True, allow=True, set_retention=use_case)
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4(), "/retention"), json={"keep_last": 0}
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_retention_policy"


def test_put_retention_unknown_server_is_404() -> None:
    use_case = _FakeUseCase(error=ServerNotFoundError("x"))
    app = _app(member=True, allow=True, set_retention=use_case)
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4(), "/retention"), json={"keep_last": 3}
    )
    assert resp.status_code == 404


def test_delete_retention_is_204_and_not_parsed_as_backup_id() -> None:
    # The literal "/backups/retention" path must route to the retention clear,
    # never be captured by the DELETE /backups/{backup_id} UUID parameter.
    use_case = _FakeUseCase()
    app = _app(member=True, allow=True, clear_retention=use_case)
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), "/retention"))
    assert resp.status_code == 204
    assert len(use_case.calls) == 1


def test_delete_retention_without_permission_is_403() -> None:
    app = _app(member=True, allow=False, clear_retention=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), "/retention"))
    assert resp.status_code == 403


def test_delete_retention_unknown_server_is_404() -> None:
    use_case = _FakeUseCase(error=ServerNotFoundError("x"))
    app = _app(member=True, allow=True, clear_retention=use_case)
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), "/retention"))
    assert resp.status_code == 404
