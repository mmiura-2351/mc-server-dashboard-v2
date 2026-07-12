"""Endpoint tests for the server-backups router (Section 6.11).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate per route (non-member -> 404, member-without-permission ->
  403, authorized member -> 2xx);
- the servers-backup-error -> HTTP-code mapping (missing 404, unsettled 409,
  restore-running 409, worker-down 503);
- create records the acting user (created_by passed through);
- list shape.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

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
    get_clear_backup_retention,
    get_create_backup,
    get_current_user,
    get_delete_backup,
    get_download_backup,
    get_global_backup_statistics,
    get_list_backups,
    get_membership_visibility,
    get_permission_checker,
    get_restore_backup,
    get_server_backup_statistics,
    get_set_backup_retention,
    get_upload_backup,
)
from mc_server_dashboard_api.servers.application.backups import (
    ListedBackup,
    RestoreResult,
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
    BackupUnsettledError,
    FileTooLargeError,
    InvalidBackupArchiveError,
    InvalidRetentionPolicyError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.audit.fakes import RecordingAuditRecorder
from tests.identity.fakes import make_user

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


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


def _app(
    *,
    member: bool,
    allow: bool,
    create: _FakeUseCase | None = None,
    list_: _FakeUseCase | None = None,
    restore: _FakeUseCase | None = None,
    delete: _FakeUseCase | None = None,
    download: _FakeUseCase | None = None,
    upload: _FakeUseCase | None = None,
    statistics: _FakeUseCase | None = None,
    global_statistics: _FakeUseCase | None = None,
    set_retention: _FakeUseCase | None = None,
    clear_retention: _FakeUseCase | None = None,
    recorder: RecordingAuditRecorder | None = None,
    is_admin: bool = False,
) -> object:
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user(
        is_platform_admin=is_admin
    )
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
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


async def _aiter(data: bytes) -> object:
    yield data


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


def test_delete_unknown_backup_is_404() -> None:
    use_case = _FakeUseCase(error=BackupNotFoundError("x"))
    app = _app(member=True, allow=True, delete=use_case)
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}"))
    assert resp.status_code == 404


# --- download (issue #281) -------------------------------------------------


def test_member_without_permission_gets_403_on_download() -> None:
    app = _app(member=True, allow=False, download=_FakeUseCase())
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/download"))
    assert resp.status_code == 403


def test_download_streams_archive_with_disposition() -> None:
    use_case = _FakeUseCase(result=_aiter(b"archive-bytes"))
    app = _app(member=True, allow=True, download=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/download"))
    assert resp.status_code == 200
    assert resp.content == b"archive-bytes"
    assert resp.headers["content-type"] == "application/gzip"
    assert "attachment" in resp.headers["content-disposition"]
    assert ".tar.gz" in resp.headers["content-disposition"]


def test_download_unknown_backup_is_404() -> None:
    use_case = _FakeUseCase(error=BackupNotFoundError("x"))
    app = _app(member=True, allow=True, download=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), f"/{uuid.uuid4()}/download"))
    assert resp.status_code == 404


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
