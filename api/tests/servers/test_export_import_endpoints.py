"""Endpoint tests for the whole-server export / import routes (issue #274).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate (non-member -> 404, member-without-permission -> 403);
- export is gated by ``file:read`` and streams ``application/zip``; a running
  server is 409 ``server_unsettled`` and audited DENIED;
- import is gated by ``server:create`` (multipart) and maps the domain errors:
  invalid metadata -> 422, name conflict -> 409, oversized -> 413,
  seed failure -> 503;
- the success audit codes (``server:export`` / ``server:import``).
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
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
    get_current_user,
    get_export_server,
    get_import_server,
    get_membership_visibility,
    get_permission_checker,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidExportMetadataError,
    RemovedExecutionBackendError,
    ServerFilesUnsettledError,
    ServerNameAlreadyExistsError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
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


class _FakeExport:
    def __init__(
        self, *, chunks: list[bytes] | None = None, error: Exception | None = None
    ) -> None:
        self._chunks = chunks or [b"zip-bytes"]
        self._error = error

    async def __call__(self, **kwargs: object) -> AsyncIterator[bytes]:
        if self._error is not None:
            raise self._error

        async def _gen() -> AsyncIterator[bytes]:
            for chunk in self._chunks:
                yield chunk

        return _gen()


class _FakeImport:
    def __init__(
        self, *, result: Server | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> Server:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _server_entity(*, community_id: uuid.UUID, name: str = "imported") -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=ServersCommunityId(community_id),
        name=ServerName(name),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
        game_port=25565,
    )


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(
    *,
    member: bool,
    allow: bool,
    export: _FakeExport | None = None,
    import_: _FakeImport | None = None,
    recorder: RecordingAuditRecorder | None = None,
) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if export is not None:
        app.dependency_overrides[get_export_server] = lambda: export
    if import_ is not None:
        app.dependency_overrides[get_import_server] = lambda: import_
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return app


def _zip_upload() -> tuple[dict[str, tuple[str, bytes, str]], dict[str, str]]:
    """Build the (files, data) pair for the import multipart POST."""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("export_metadata.json", b"{}")
    files = {"file": ("server.zip", buf.getvalue(), "application/zip")}
    data = {"name": "imported", "execution_backend": "host_process"}
    return files, data


# --- export ----------------------------------------------------------------


def test_non_member_gets_404_on_export() -> None:
    app = _app(member=False, allow=True, export=_FakeExport())
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}/export")
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_export() -> None:
    app = _app(member=True, allow=False, export=_FakeExport())
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}/export")
    assert resp.status_code == 403


def test_export_streams_zip_and_audits() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        export=_FakeExport(chunks=[b"zip", b"-bytes"]),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.content == b"zip-bytes"
    assert [e.operation for e in recorder.events] == [ops.SERVER_EXPORT]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_SERVER


def test_export_running_is_409_and_audits_denied() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        export=_FakeExport(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}/export")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"
    assert [e.operation for e in recorder.events] == [ops.SERVER_EXPORT]
    assert recorder.events[0].outcome is Outcome.DENIED


# --- import ----------------------------------------------------------------


def test_non_member_gets_404_on_import() -> None:
    app = _app(member=False, allow=True, import_=_FakeImport())
    client = next(_client(app))
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_import() -> None:
    app = _app(member=True, allow=False, import_=_FakeImport())
    client = next(_client(app))
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 403


def test_import_creates_server_and_audits() -> None:
    community = uuid.uuid4()
    recorder = RecordingAuditRecorder()
    use_case = _FakeImport(result=_server_entity(community_id=community))
    app = _app(member=True, allow=True, import_=use_case, recorder=recorder)
    client = next(_client(app))
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{community}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "imported"
    assert body["game_port"] == 25565
    # The name and backend come from the request form, not the metadata.
    assert use_case.calls[0]["name"] == "imported"
    assert use_case.calls[0]["execution_backend"] == "host_process"
    assert [e.operation for e in recorder.events] == [ops.SERVER_IMPORT]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_SERVER


def test_import_invalid_metadata_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=InvalidExportMetadataError("bad")),
    )
    client = next(_client(app))
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_export_metadata"


def test_import_removed_backend_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=RemovedExecutionBackendError("host_process")),
    )
    client = next(_client(app))
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "removed_execution_backend"


def test_import_name_conflict_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=ServerNameAlreadyExistsError("imported")),
    )
    client = next(_client(app))
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_name_exists"


def test_import_oversized_is_413() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=FileTooLargeError("9999")),
    )
    client = next(_client(app))
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 413


def test_import_seed_failure_is_503() -> None:
    app = _app(
        member=True,
        allow=True,
        import_=_FakeImport(error=WorkingSetSeedFailedError("x")),
    )
    client = next(_client(app))
    files, data = _zip_upload()
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/servers/import",
        files=files,
        data=data,
    )
    assert resp.status_code == 503
    assert resp.json()["reason"] == "seed_failed"
