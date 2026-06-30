"""Endpoint tests for the server-files router (Section 6.10).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate per route (non-member -> 404, member-without-permission ->
  403, authorized member -> 2xx);
- the servers-file-error -> HTTP-code mapping (missing 404, traversal 422,
  oversized 413, transitional 409, disconnected worker 503);
- base64 bytes-faithful read/write;
- per-resource gating with the real role+grant checker: a per-resource
  ``file:read`` grant on server X opens exactly X's files.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.community.adapters.permission_checker import (
    RepositoryMembershipVisibility,
    RoleGrantPermissionChecker,
)
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
    get_delete_file,
    get_download_file,
    get_list_dir,
    get_list_file_versions,
    get_make_dir,
    get_membership_visibility,
    get_permission_checker,
    get_read_file,
    get_read_file_version,
    get_rename_file,
    get_rollback_file,
    get_search_files,
    get_upload_file,
    get_write_file,
)
from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.application.files import (
    DirListing,
    SearchResult,
    WriteFile,
)
from mc_server_dashboard_api.servers.domain.control_plane import WorkerUnavailableError
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
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
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServerCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
    ObservedState,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    ServerId as ServerScopeId,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from tests.audit.fakes import RecordingAuditRecorder
from tests.community.fakes import FakeAuthzUnitOfWork
from tests.identity.fakes import make_user
from tests.servers.fakes import FakeUnitOfWork

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


class _SetChecker(PermissionChecker):
    """Grants only the listed permission codes (per-permission gate tests)."""

    def __init__(self, *, allowed: set[str]) -> None:
        self._allowed = allowed

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        return operation.value in self._allowed


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


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


class _FakeUpload:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error


class _FakeDownload:
    """Fake :class:`DownloadFile` with its file/dir method surface."""

    def __init__(
        self,
        *,
        is_dir: bool = False,
        file_content: bytes = b"",
        zip_chunks: list[bytes] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._is_dir = is_dir
        self._file_content = file_content
        self._zip_chunks = zip_chunks or [b"zip"]
        self._error = error
        self.calls: list[str] = []

    async def is_dir(self, **kwargs: object) -> bool:
        self.calls.append("is_dir")
        if self._error is not None:
            raise self._error
        return self._is_dir

    async def file_stream(self, **kwargs: object) -> object:
        self.calls.append("file_stream")
        content = self._file_content

        async def _gen() -> object:
            # Yield in two chunks (when non-empty) so the route's StreamingResponse
            # is exercised as a real stream, not a single buffered blob (#265).
            half = len(content) // 2
            if half:
                yield content[:half]
                yield content[half:]
            elif content:
                yield content

        return _gen()

    async def file_size(self, **kwargs: object) -> int | None:
        self.calls.append("file_size")
        return len(self._file_content)

    async def dir_zip(self, **kwargs: object) -> object:
        self.calls.append("dir_zip")

        async def _gen() -> object:
            for chunk in self._zip_chunks:
                yield chunk

        return _gen()


def _app(
    *,
    member: bool,
    allow: bool,
    permissions: set[str] | None = None,
    read: _FakeUseCase | None = None,
    list_: _FakeUseCase | None = None,
    write: _FakeUseCase | None = None,
    history: _FakeUseCase | None = None,
    version: _FakeUseCase | None = None,
    rollback: _FakeUseCase | None = None,
    upload: _FakeUpload | None = None,
    download: _FakeDownload | None = None,
    rename: _FakeUseCase | None = None,
    delete: _FakeUseCase | None = None,
    mkdir: _FakeUseCase | None = None,
    search: _FakeUseCase | None = None,
    recorder: RecordingAuditRecorder | None = None,
) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    if permissions is not None:
        app.dependency_overrides[get_permission_checker] = lambda: _SetChecker(
            allowed=permissions
        )
    else:
        app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(
            allow=allow
        )
    if read is not None:
        app.dependency_overrides[get_read_file] = lambda: read
    if list_ is not None:
        app.dependency_overrides[get_list_dir] = lambda: list_
    if write is not None:
        app.dependency_overrides[get_write_file] = lambda: write
    if history is not None:
        app.dependency_overrides[get_list_file_versions] = lambda: history
    if version is not None:
        app.dependency_overrides[get_read_file_version] = lambda: version
    if rollback is not None:
        app.dependency_overrides[get_rollback_file] = lambda: rollback
    if upload is not None:
        app.dependency_overrides[get_upload_file] = lambda: upload
    if download is not None:
        app.dependency_overrides[get_download_file] = lambda: download
    if rename is not None:
        app.dependency_overrides[get_rename_file] = lambda: rename
    if delete is not None:
        app.dependency_overrides[get_delete_file] = lambda: delete
    if mkdir is not None:
        app.dependency_overrides[get_make_dir] = lambda: mkdir
    if search is not None:
        app.dependency_overrides[get_search_files] = lambda: search
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return app


def _url(community: uuid.UUID, server: uuid.UUID, suffix: str = "") -> str:
    return f"/api/communities/{community}/servers/{server}/files{suffix}"


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_read() -> None:
    app = _app(member=False, allow=True, read=_FakeUseCase(result=b""))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_write() -> None:
    app = _app(member=True, allow=False, write=_FakeUseCase())
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "f"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 403


# --- read / write happy paths (bytes-faithful base64) ----------------------


def test_read_returns_base64_content() -> None:
    raw = bytes(range(256))  # non-UTF-8 bytes prove no encoding mangling
    app = _app(member=True, allow=True, read=_FakeUseCase(result=raw))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "level.dat"})
    assert resp.status_code == 200
    body = resp.json()
    assert base64.b64decode(body["content_base64"]) == raw
    assert body["path"] == "level.dat"


def test_list_returns_entries() -> None:
    use_case = _FakeUseCase(
        result=DirListing(entries=[FileEntry(name="world", is_dir=True, size=0)])
    )
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": ".", "list": "true"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == [{"name": "world", "is_dir": True, "size": 0}]
    assert body["truncated"] is False


def test_list_surfaces_truncated_flag() -> None:
    use_case = _FakeUseCase(
        result=DirListing(
            entries=[FileEntry(name="world", is_dir=True, size=0)], truncated=True
        )
    )
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": ".", "list": "true"}
    )
    assert resp.status_code == 200
    assert resp.json()["truncated"] is True


def test_list_disconnected_worker_is_503() -> None:
    app = _app(
        member=True, allow=True, list_=_FakeUseCase(error=WorkerUnavailableError("x"))
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": ".", "list": "true"}
    )
    assert resp.status_code == 503
    assert resp.json()["reason"] == "worker_unavailable"


def test_list_transitional_server_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        list_=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": ".", "list": "true"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_write_server_busy_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        write=_FakeUseCase(error=ServerBusyError("s")),
    )
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "level.dat"},
        json={"content_base64": "dGVzdA=="},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_busy"


def test_write_decodes_base64_and_passes_bytes() -> None:
    raw = bytes(range(256))
    use_case = _FakeUseCase()
    app = _app(member=True, allow=True, write=use_case)
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "level.dat"},
        json={"content_base64": base64.b64encode(raw).decode("ascii")},
    )
    assert resp.status_code == 204
    assert use_case.calls[0]["content"] == raw


def test_write_invalid_base64_is_422() -> None:
    app = _app(member=True, allow=True, write=_FakeUseCase())
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "f"},
        json={"content_base64": "not!base64!"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_base64"


# --- error mapping ---------------------------------------------------------


def test_read_missing_file_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        read=_FakeUseCase(error=ServerFileNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 404


def test_read_missing_server_is_404() -> None:
    app = _app(
        member=True, allow=True, read=_FakeUseCase(error=ServerNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 404


def test_read_traversal_is_422() -> None:
    app = _app(
        member=True, allow=True, read=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "../escape"})
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"


def test_read_is_a_directory_surfaces_reason() -> None:
    # A running-server read of a directory (issue #548): the refined reason rides
    # through to the 422 body rather than the misleading invalid_path.
    app = _app(
        member=True,
        allow=True,
        read=_FakeUseCase(error=InvalidFilePathError("x", reason="is_a_directory")),
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "config"})
    assert resp.status_code == 422
    assert resp.json()["reason"] == "is_a_directory"


def test_read_payload_too_large_is_413() -> None:
    # A running-server read past the control-plane cap (issue #548) -> 413.
    app = _app(member=True, allow=True, read=_FakeUseCase(error=FileTooLargeError("x")))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "big.bin"})
    assert resp.status_code == 413
    assert resp.json()["reason"] == "file_too_large"


def test_list_not_a_directory_surfaces_reason() -> None:
    app = _app(
        member=True,
        allow=True,
        list_=_FakeUseCase(error=InvalidFilePathError("x", reason="not_a_directory")),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "server.properties", "list": "true"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "not_a_directory"


def test_read_transitional_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        read=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_read_disconnected_worker_is_503() -> None:
    app = _app(
        member=True, allow=True, read=_FakeUseCase(error=WorkerUnavailableError("x"))
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 503
    assert resp.json()["reason"] == "worker_unavailable"


def test_write_oversized_is_413() -> None:
    app = _app(
        member=True, allow=True, write=_FakeUseCase(error=FileTooLargeError("x"))
    )
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "f"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 413
    assert resp.json()["reason"] == "file_too_large"


def test_write_traversal_is_422() -> None:
    app = _app(
        member=True, allow=True, write=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "../escape"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"


def test_write_symlink_refused_surfaces_reason() -> None:
    # A running-server write onto a refused symlink (issue #548) -> 422 with the
    # honest reason rather than invalid_path.
    app = _app(
        member=True,
        allow=True,
        write=_FakeUseCase(error=InvalidFilePathError("x", reason="symlink_refused")),
    )
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "link"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "symlink_refused"


# --- history / rollback ----------------------------------------------------


def test_history_lists_versions() -> None:
    app = _app(member=True, allow=True, history=_FakeUseCase(result=["v2", "v1"]))
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/history"), params={"path": "f"}
    )
    assert resp.status_code == 200
    assert resp.json()["versions"] == ["v2", "v1"]


def test_version_returns_base64_content() -> None:
    raw = bytes(range(256))  # non-UTF-8 bytes prove no encoding mangling
    app = _app(member=True, allow=True, version=_FakeUseCase(result=raw))
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/version"),
        params={"path": "f", "version_id": "v1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert base64.b64decode(body["content_base64"]) == raw
    assert body["path"] == "f"


def test_version_passes_path_and_version_id() -> None:
    use_case = _FakeUseCase(result=b"old")
    app = _app(member=True, allow=True, version=use_case)
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/version"),
        params={"path": "server.properties", "version_id": "v2"},
    )
    assert resp.status_code == 200
    assert use_case.calls[0]["rel_path"] == "server.properties"
    assert use_case.calls[0]["version_id"] == "v2"


def test_version_allowed_with_file_read() -> None:
    # The preview returns file content, so file:read (not file:history) gates it.
    app = _app(
        member=True,
        allow=False,
        permissions={"file:read"},
        version=_FakeUseCase(result=b"old"),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/version"),
        params={"path": "f", "version_id": "v1"},
    )
    assert resp.status_code == 200


def test_version_forbidden_with_file_history_only() -> None:
    # file:history lists versions but does not grant content access; reading a
    # historical version's bytes still requires file:read (else 403).
    app = _app(
        member=True,
        allow=False,
        permissions={"file:history"},
        version=_FakeUseCase(result=b"old"),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/version"),
        params={"path": "f", "version_id": "v1"},
    )
    assert resp.status_code == 403


def test_version_unknown_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        version=_FakeUseCase(error=ServerFileNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/version"),
        params={"path": "f", "version_id": "missing"},
    )
    assert resp.status_code == 404


def test_version_traversal_is_422() -> None:
    app = _app(
        member=True, allow=True, version=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/version"),
        params={"path": "../escape", "version_id": "v1"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"


def test_version_malformed_version_id_is_422() -> None:
    # A version_id outside the retained-version charset is bad client input, not an
    # internal fault: 422 invalid_version_id, never a 500 (issue #1527).
    app = _app(
        member=True,
        allow=True,
        version=_FakeUseCase(error=InvalidVersionIdError("bad.id")),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/version"),
        params={"path": "f", "version_id": "bad.id"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_version_id"


def test_rollback_success_is_204() -> None:
    use_case = _FakeUseCase()
    app = _app(member=True, allow=True, rollback=use_case)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rollback"),
        params={"path": "f"},
        json={"version_id": "v1"},
    )
    assert resp.status_code == 204
    assert use_case.calls[0]["version_id"] == "v1"


def test_rollback_while_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        rollback=_FakeUseCase(error=ServerNotStoppedError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rollback"),
        params={"path": "f"},
        json={"version_id": "v1"},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_not_stopped"


def test_rollback_malformed_version_id_is_422() -> None:
    # Same posture as the preview route: a malformed version_id is 422
    # invalid_version_id, never a 500 (issue #1527).
    app = _app(
        member=True,
        allow=True,
        rollback=_FakeUseCase(error=InvalidVersionIdError("bad.id")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rollback"),
        params={"path": "f"},
        json={"version_id": "bad.id"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_version_id"


# --- upload ----------------------------------------------------------------


def test_upload_single_file_is_204() -> None:
    upload = _FakeUpload()
    app = _app(member=True, allow=True, upload=upload)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        params={"path": "plugins"},
        files={"file": ("mod.jar", b"jar-bytes", "application/java-archive")},
    )
    assert resp.status_code == 204
    assert upload.calls[0]["filename"] == "mod.jar"
    assert upload.calls[0]["content"] == b"jar-bytes"
    assert upload.calls[0]["dir_path"] == "plugins"
    assert upload.calls[0]["extract"] is False


def test_upload_extract_flag_passed() -> None:
    upload = _FakeUpload()
    app = _app(member=True, allow=True, upload=upload)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        params={"path": ".", "extract": "true"},
        files={"file": ("pack.zip", b"zip-bytes", "application/zip")},
    )
    assert resp.status_code == 204
    assert upload.calls[0]["extract"] is True


def test_upload_requires_file_edit_permission() -> None:
    app = _app(member=True, allow=False, upload=_FakeUpload())
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        files={"file": ("f", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 403


def test_upload_traversal_filename_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        upload=_FakeUpload(error=InvalidFilePathError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        files={"file": ("f", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"


def test_upload_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        upload=_FakeUpload(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        files={"file": ("f", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_upload_over_cap_is_413() -> None:
    app = _app(
        member=True, allow=True, upload=_FakeUpload(error=FileTooLargeError("x"))
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        files={"file": ("f", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 413
    assert resp.json()["reason"] == "file_too_large"


# --- download --------------------------------------------------------------


def test_download_file_returns_bytes() -> None:
    raw = bytes(range(256))
    app = _app(
        member=True, allow=True, download=_FakeDownload(is_dir=False, file_content=raw)
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "level.dat"},
    )
    assert resp.status_code == 200
    assert resp.content == raw
    # The single-file branch streams (issue #265) with a Content-Length from the
    # cheap size lookup when known.
    assert resp.headers["content-length"] == str(len(raw))
    cd = resp.headers["content-disposition"]
    assert cd.startswith("attachment; ")
    assert 'filename="level.dat"' in cd
    assert "filename*=UTF-8''level.dat" in cd


def test_download_dir_returns_zip_stream() -> None:
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(is_dir=True, zip_chunks=[b"PK", b"zip-tail"]),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "world"},
    )
    assert resp.status_code == 200
    assert resp.content == b"PKzip-tail"
    assert resp.headers["content-type"] == "application/zip"
    cd = resp.headers["content-disposition"]
    assert 'filename="world.zip"' in cd
    assert "filename*=UTF-8''world.zip" in cd


def test_download_requires_file_read_permission() -> None:
    app = _app(member=True, allow=False, download=_FakeDownload())
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"), params={"path": "f"}
    )
    assert resp.status_code == 403


def test_download_missing_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=ServerFileNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"), params={"path": "f"}
    )
    assert resp.status_code == 404


def test_download_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"), params={"path": "f"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


# --- download Content-Disposition (RFC 6266 / 5987) ------------------------


def test_download_filename_with_quote_is_sanitized() -> None:
    # A file named evil".zip must not break out of the quoted-string and inject
    # extra Content-Disposition parameters; the quote is replaced in the ASCII
    # fallback and the real name is carried percent-encoded in filename*.
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=False))
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"), params={"path": 'evil".zip'}
    )
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    # The fallback must be a clean quoted-string with no embedded quote, so the
    # crafted name cannot inject extra parameters.
    assert cd == "attachment; filename=\"evil_.zip\"; filename*=UTF-8''evil%22.zip"


def test_download_unicode_filename_does_not_500() -> None:
    # A legitimate Unicode name (ワールド.zip) used to 500 when Starlette latin-1
    # encoded the header; it now succeeds with an ASCII fallback + UTF-8 filename*.
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=False))
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "ワールド.zip"},
    )
    assert resp.status_code == 200
    cd = resp.headers["content-disposition"]
    assert 'filename="____.zip"' in cd  # 4 non-ASCII kana -> 4 underscores
    assert "filename*=UTF-8''" in cd
    assert "%E3%83" in cd  # UTF-8 percent-encoding of the kana


# --- upload streaming cap (413 before full buffer) -------------------------


def test_upload_over_cap_body_is_413_before_use_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The route counts the multipart body in chunks and aborts past the cap
    # before the use case is invoked. Patch the cap small so the fixture stays
    # tiny; the streamed loop trips on it.
    import mc_server_dashboard_api.servers.api.files as files_module

    monkeypatch.setattr(files_module, "MAX_UPLOAD_BYTES", 16)
    upload = _FakeUpload()
    app = _app(member=True, allow=True, upload=upload)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        files={"file": ("big.bin", b"x" * 1024, "application/octet-stream")},
    )
    assert resp.status_code == 413
    assert resp.json()["reason"] == "file_too_large"
    assert upload.calls == []  # aborted before the use case ran


# --- audit recording -------------------------------------------------------


def test_write_success_records_file_write_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, write=_FakeUseCase(), recorder=recorder)
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "level.dat"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.FILE_WRITE]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_FILE


def test_write_unsettled_records_denied_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        write=_FakeUseCase(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "f"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 409
    assert [e.operation for e in recorder.events] == [ops.FILE_WRITE]
    assert recorder.events[0].outcome is Outcome.DENIED


def test_write_validation_failure_is_not_audited() -> None:
    # 422 (invalid path) raises before the audit record, matching the existing
    # posture: validation rejects are not audited.
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        write=_FakeUseCase(error=InvalidFilePathError("x")),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "../escape"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 422
    assert recorder.events == []


def test_rollback_success_records_file_rollback_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, rollback=_FakeUseCase(), recorder=recorder)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rollback"),
        params={"path": "f"},
        json={"version_id": "v1"},
    )
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.FILE_ROLLBACK]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_FILE


def test_rollback_while_running_records_denied_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        rollback=_FakeUseCase(error=ServerNotStoppedError("x")),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rollback"),
        params={"path": "f"},
        json={"version_id": "v1"},
    )
    assert resp.status_code == 409
    assert [e.operation for e in recorder.events] == [ops.FILE_ROLLBACK]
    assert recorder.events[0].outcome is Outcome.DENIED


def test_upload_success_records_file_upload_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, upload=_FakeUpload(), recorder=recorder)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        files={"file": ("mod.jar", b"jar", "application/octet-stream")},
    )
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.FILE_UPLOAD]
    assert recorder.events[0].outcome is Outcome.SUCCESS
    assert recorder.events[0].target_type == ops.TARGET_FILE


def test_upload_unsettled_records_denied_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        upload=_FakeUpload(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        files={"file": ("f", b"x", "application/octet-stream")},
    )
    assert resp.status_code == 409
    assert [e.operation for e in recorder.events] == [ops.FILE_UPLOAD]
    assert recorder.events[0].outcome is Outcome.DENIED


def test_download_success_records_file_download_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(is_dir=False, file_content=b"x"),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"), params={"path": "f"}
    )
    assert resp.status_code == 200
    assert [e.operation for e in recorder.events] == [ops.FILE_DOWNLOAD]
    assert recorder.events[0].outcome is Outcome.SUCCESS


def test_download_unsettled_records_denied_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"), params={"path": "f"}
    )
    assert resp.status_code == 409
    assert [e.operation for e in recorder.events] == [ops.FILE_DOWNLOAD]
    assert recorder.events[0].outcome is Outcome.DENIED


def test_download_validation_failure_is_not_audited() -> None:
    # 422 (invalid path) raises before the audit record, matching the existing
    # posture: validation rejects are not audited.
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=InvalidFilePathError("x")),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"), params={"path": "../escape"}
    )
    assert resp.status_code == 422
    assert recorder.events == []


# --- rename (issue #259) ---------------------------------------------------


def test_rename_is_204_and_passes_paths() -> None:
    rename = _FakeUseCase()
    app = _app(member=True, allow=True, rename=rename)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "old.txt", "to": "new.txt"},
    )
    assert resp.status_code == 204
    assert rename.calls[0]["from_path"] == "old.txt"
    assert rename.calls[0]["to_path"] == "new.txt"


def test_rename_requires_file_edit_permission() -> None:
    app = _app(member=True, allow=False, rename=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "a", "to": "b"},
    )
    assert resp.status_code == 403


def test_rename_missing_source_is_404() -> None:
    app = _app(
        member=True, allow=True, rename=_FakeUseCase(error=ServerFileNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"), json={"from": "a", "to": "b"}
    )
    assert resp.status_code == 404


def test_rename_existing_destination_is_409() -> None:
    app = _app(
        member=True, allow=True, rename=_FakeUseCase(error=FileAlreadyExistsError("b"))
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"), json={"from": "a", "to": "b"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "destination_exists"


def test_rename_traversal_is_422() -> None:
    app = _app(
        member=True, allow=True, rename=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "a", "to": "../escape"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"


def test_rename_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        rename=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"), json={"from": "a", "to": "b"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_rename_success_records_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, rename=_FakeUseCase(), recorder=recorder)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"), json={"from": "a", "to": "b"}
    )
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.FILE_RENAME]
    assert recorder.events[0].outcome is Outcome.SUCCESS


def test_rename_unsettled_records_denied_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        rename=_FakeUseCase(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"), json={"from": "a", "to": "b"}
    )
    assert resp.status_code == 409
    assert [e.operation for e in recorder.events] == [ops.FILE_RENAME]
    assert recorder.events[0].outcome is Outcome.DENIED


# --- delete (issue #259) ---------------------------------------------------


def test_delete_is_204_and_passes_path() -> None:
    delete = _FakeUseCase()
    app = _app(member=True, allow=True, delete=delete)
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "old.txt"})
    assert resp.status_code == 204
    assert delete.calls[0]["rel_path"] == "old.txt"


def test_delete_requires_file_edit_permission() -> None:
    app = _app(member=True, allow=False, delete=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 403


def test_delete_missing_is_404() -> None:
    app = _app(
        member=True, allow=True, delete=_FakeUseCase(error=ServerFileNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 404


def test_delete_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        delete=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_delete_success_records_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, delete=_FakeUseCase(), recorder=recorder)
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.FILE_DELETE]
    assert recorder.events[0].outcome is Outcome.SUCCESS


# --- mkdir (issue #259) ----------------------------------------------------


def test_mkdir_is_204_and_passes_path() -> None:
    mkdir = _FakeUseCase()
    app = _app(member=True, allow=True, mkdir=mkdir)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "plugins"}
    )
    assert resp.status_code == 204
    assert mkdir.calls[0]["rel_path"] == "plugins"


def test_mkdir_requires_file_edit_permission() -> None:
    app = _app(member=True, allow=False, mkdir=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "p"}
    )
    assert resp.status_code == 403


def test_mkdir_traversal_is_422() -> None:
    app = _app(
        member=True, allow=True, mkdir=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "../escape"}
    )
    assert resp.status_code == 422


def test_mkdir_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        mkdir=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "p"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_mkdir_success_records_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, mkdir=_FakeUseCase(), recorder=recorder)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "p"}
    )
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.FILE_MKDIR]


# --- search (issue #259) ---------------------------------------------------


def test_search_returns_paths_and_truncated() -> None:
    result = SearchResult(paths=["config/ops.json"], truncated=True)
    search = _FakeUseCase(result=result)
    app = _app(member=True, allow=True, search=search)
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/search"),
        json={"query": "ops", "by": "name", "max_results": 50},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["paths"] == ["config/ops.json"]
    assert body["truncated"] is True
    assert search.calls[0]["query"] == "ops"
    assert search.calls[0]["by"] == "name"
    assert search.calls[0]["max_results"] == 50


def test_search_requires_file_read_permission() -> None:
    app = _app(member=True, allow=False, search=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/search"), json={"query": "x"})
    assert resp.status_code == 403


def test_search_invalid_by_is_422() -> None:
    app = _app(
        member=True, allow=True, search=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/search"),
        json={"query": "x", "by": "regex"},
    )
    assert resp.status_code == 422


def test_search_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        search=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/search"), json={"query": "x"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_search_success_records_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        search=_FakeUseCase(result=SearchResult(paths=[], truncated=False)),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/search"), json={"query": "x"})
    assert resp.status_code == 200
    assert [e.operation for e in recorder.events] == [ops.FILE_SEARCH]
    assert recorder.events[0].outcome is Outcome.SUCCESS


# --- per-resource grant (real checker) -------------------------------------


def test_file_read_grant_on_one_server_opens_exactly_that_server() -> None:
    user_id = uuid.uuid4()
    community = uuid.uuid4()
    server_x = uuid.uuid4()
    server_y = uuid.uuid4()

    authz_uow = FakeAuthzUnitOfWork()
    user = UserId(user_id)
    com = CommunityId(community)
    # Member with no role permissions, but a per-resource file:read grant on X.
    authz_uow.add_role(user, com, set())
    authz_uow.add_grant(user, com, "server", server_x, {Permission("file:read")})

    app = create_app()
    user_obj = make_user()
    user_obj.id = type(user_obj.id)(user_id)
    app.dependency_overrides[get_current_user] = lambda: user_obj
    app.dependency_overrides[get_membership_visibility] = lambda: (
        RepositoryMembershipVisibility(authz_uow)
    )
    app.dependency_overrides[get_permission_checker] = lambda: (
        RoleGrantPermissionChecker(authz_uow)
    )
    app.dependency_overrides[get_read_file] = lambda: _FakeUseCase(result=b"ok")
    client = next(_client(app))

    opened = client.get(_url(community, server_x), params={"path": "f"})
    assert opened.status_code == 200

    blocked = client.get(_url(community, server_y), params={"path": "f"})
    assert blocked.status_code == 403


# --- RelPath control-char hardening, end-to-end edge (issue #266) -----------


_NOW_SERVER = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def _stopped_server(community: uuid.UUID, server: uuid.UUID) -> Server:
    return Server(
        id=ServerScopeId(server),
        community_id=ServerCommunityId(community),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW_SERVER,
        assigned_worker_id=None,
        created_at=_NOW_SERVER,
        updated_at=_NOW_SERVER,
    )


def _real_write_app(
    tmp_path: object, community: uuid.UUID, server: uuid.UUID
) -> object:
    """Wire the write route to a REAL WriteFile over real fs Storage.

    Drives the actual RelPath through the seam (rather than a faked use case), so a
    control-character path is rejected by RelPath and surfaces as a 422 at the edge
    exactly as a traversal path does.
    """

    uow = FakeUnitOfWork()
    uow.servers.seed(_stopped_server(community, server))
    file_store = StorageFileStoreAdapter(storage=FsStorage(tmp_path))  # type: ignore[arg-type]
    use_case = WriteFile(uow=uow, control_plane=None, file_store=file_store)  # type: ignore[arg-type]

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=True
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=True)
    app.dependency_overrides[get_write_file] = lambda: use_case
    return app


@pytest.mark.parametrize("bad", ["foo\x00bar", "config\r\nx", "a\x1fb"])
def test_write_control_char_path_is_422(tmp_path: object, bad: str) -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _real_write_app(tmp_path, community, server)
    client = next(_client(app))

    resp = client.put(
        _url(community, server),
        params={"path": bad},
        json={"content_base64": ""},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"


def test_write_unicode_path_is_accepted(tmp_path: object) -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _real_write_app(tmp_path, community, server)
    client = next(_client(app))

    resp = client.put(
        _url(community, server),
        params={"path": "世界/レベル.dat"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 204
