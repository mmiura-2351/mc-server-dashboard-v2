"""Endpoint tests for the server-files router (Section 6.10).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate per route (non-member -> 404, member-without-permission ->
  403, authorized member -> 2xx);
- the servers-file-error -> HTTP-code mapping (missing 404, traversal 422,
  oversized 413, transitional 409, disconnected worker 503);
- base64 bytes-faithful read/write;
- per-resource gating with the real role+grant checker: a per-resource
  ``file:read`` grant on server X opens exactly X's files;
- the file download grant (issue #2352): the mint's gate and pre-flight,
  redemption without an ``Authorization`` header, and the grant's binding to one
  ``?path=``.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    get_authenticate_download_grant,
    get_authenticate_request,
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
    get_token_service,
    get_upload_file,
    get_write_file,
)
from mc_server_dashboard_api.download_cookie import DOWNLOAD_COOKIE_NAME
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.application.authenticate_download_grant import (
    AuthenticateDownloadGrant,
)
from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.application.export_import import (
    export_download_grant_resource,
)
from mc_server_dashboard_api.servers.application.files import (
    DirListing,
    SearchResult,
    WriteFile,
    file_download_grant_resource,
)
from mc_server_dashboard_api.servers.domain.control_plane import WorkerUnavailableError
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileAlreadyExistsError,
    FileTooLargeError,
    InvalidFilePathError,
    InvalidVersionIdError,
    PlatformManagedKeyError,
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
from tests.client_utils import enter_client
from tests.community.fakes import FakeAuthzUnitOfWork
from tests.identity.fakes import FakeClock, make_user
from tests.identity.fakes import FakeUnitOfWork as IdentityFakeUnitOfWork
from tests.servers.fakes import FakeUnitOfWork

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


def _client(app: object) -> TestClient:
    return enter_client(TestClient(app))  # type: ignore[arg-type]


class _FakeUpload:
    """Fake :class:`UploadFile`, carrying its exact call signature (#2522)."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        community_id: ServerCommunityId,
        server_id: ServerScopeId,
        dir_path: str,
        filename: str,
        content: bytes,
        extract: bool,
    ) -> None:
        self.calls.append(
            {
                "community_id": community_id,
                "server_id": server_id,
                "dir_path": dir_path,
                "filename": filename,
                "content": content,
                "extract": extract,
            }
        )
        if self._error is not None:
            raise self._error


class _FakeDownload:
    """Fake :class:`DownloadFile` with its file/dir method surface.

    The four methods mirror the real class's signatures exactly, down to the ids
    this double never reads (#2522); ``_app``'s ``download`` parameter names this
    class, so a shapeless stand-in cannot be substituted for it.
    """

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

    async def is_dir(
        self,
        *,
        community_id: ServerCommunityId,
        server_id: ServerScopeId,
        rel_path: str,
    ) -> bool:
        self.calls.append("is_dir")
        if self._error is not None:
            raise self._error
        return self._is_dir

    async def file_stream(
        self,
        *,
        community_id: ServerCommunityId,
        server_id: ServerScopeId,
        rel_path: str,
    ) -> AsyncIterator[bytes]:
        self.calls.append("file_stream")
        content = self._file_content

        async def _gen() -> AsyncIterator[bytes]:
            # Yield in two chunks (when non-empty) so the route's StreamingResponse
            # is exercised as a real stream, not a single buffered blob (#265).
            half = len(content) // 2
            if half:
                yield content[:half]
                yield content[half:]
            elif content:
                yield content

        return _gen()

    async def file_size(
        self,
        *,
        community_id: ServerCommunityId,
        server_id: ServerScopeId,
        rel_path: str,
    ) -> int | None:
        self.calls.append("file_size")
        return len(self._file_content)

    async def dir_zip(
        self,
        *,
        community_id: ServerCommunityId,
        server_id: ServerScopeId,
        rel_path: str,
    ) -> AsyncIterator[bytes]:
        self.calls.append("dir_zip")

        async def _gen() -> AsyncIterator[bytes]:
            for chunk in self._zip_chunks:
                yield chunk

        return _gen()


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
    permissions: set[str] | None = None,
    subject: User | None = None,
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
    global _clock, _tokens, _user
    app = _shared_app
    app.dependency_overrides.clear()
    _user = subject if subject is not None else make_user()
    _clock = FakeClock(_NOW)
    _tokens = JwtTokenService(
        signing_key=_SIGNING_KEY,
        algorithm="HS256",
        access_ttl=dt.timedelta(minutes=15),
        download_grant_ttl=dt.timedelta(seconds=_GRANT_TTL_SECONDS),
        download_cookie_ttl=dt.timedelta(seconds=_COOKIE_TTL_SECONDS),
        clock=_clock,
    )
    identity_uow = IdentityFakeUnitOfWork()
    identity_uow.users.seed(_user)
    app.dependency_overrides[get_current_user] = lambda: _user
    # The file download resolves its subject itself (Bearer *or* grant), so it goes
    # through these two use cases rather than get_current_user.
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


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {_tokens.issue_access_token(_user.id)}"}


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_read() -> None:
    app = _app(member=False, allow=True, read=_FakeUseCase(result=b""))
    client = _client(app)
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_write() -> None:
    app = _app(member=True, allow=False, write=_FakeUseCase())
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": ".", "list": "true"}
    )
    assert resp.status_code == 200
    assert resp.json()["truncated"] is True


def test_list_disconnected_worker_is_503() -> None:
    app = _app(
        member=True, allow=True, list_=_FakeUseCase(error=WorkerUnavailableError("x"))
    )
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "level.dat"},
        json={"content_base64": base64.b64encode(raw).decode("ascii")},
    )
    assert resp.status_code == 204
    assert use_case.calls[0]["content"] == raw


def test_write_invalid_base64_is_422() -> None:
    app = _app(member=True, allow=True, write=_FakeUseCase())
    client = _client(app)
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
    client = _client(app)
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 404


def test_read_missing_server_is_404() -> None:
    app = _app(
        member=True, allow=True, read=_FakeUseCase(error=ServerNotFoundError("x"))
    )
    client = _client(app)
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 404


def test_read_traversal_is_422() -> None:
    app = _app(
        member=True, allow=True, read=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = _client(app)
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
    client = _client(app)
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "config"})
    assert resp.status_code == 422
    assert resp.json()["reason"] == "is_a_directory"


def test_read_symlink_refused_surfaces_reason() -> None:
    # The reason both branches now produce for a path-component symlink: the Worker
    # for a running server, Storage for one at rest (issue #2432). It must ride
    # through to the 422 body, because that reason is what selects the browser's
    # "Symbolic links are not allowed." sentence -- the one answer the operator gets
    # for the same click in either state.
    app = _app(
        member=True,
        allow=True,
        read=_FakeUseCase(error=InvalidFilePathError("x", reason="symlink_refused")),
    )
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": "alias/inner.txt"}
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "symlink_refused"


def test_read_payload_too_large_is_413() -> None:
    # A running-server read past the control-plane cap (issue #548) -> 413.
    app = _app(member=True, allow=True, read=_FakeUseCase(error=FileTooLargeError("x")))
    client = _client(app)
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "big.bin"})
    assert resp.status_code == 413
    assert resp.json()["reason"] == "file_too_large"


def test_list_not_a_directory_surfaces_reason() -> None:
    app = _app(
        member=True,
        allow=True,
        list_=_FakeUseCase(error=InvalidFilePathError("x", reason="not_a_directory")),
    )
    client = _client(app)
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
    client = _client(app)
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_read_disconnected_worker_is_503() -> None:
    app = _app(
        member=True, allow=True, read=_FakeUseCase(error=WorkerUnavailableError("x"))
    )
    client = _client(app)
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 503
    assert resp.json()["reason"] == "worker_unavailable"


def test_write_oversized_is_413() -> None:
    app = _app(
        member=True, allow=True, write=_FakeUseCase(error=FileTooLargeError("x"))
    )
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "link"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "symlink_refused"


def test_write_name_too_long_surfaces_reason() -> None:
    # An over-long destination name is a modelled 422, not a bare ENAMETOOLONG 500
    # (issue #2433).
    app = _app(
        member=True,
        allow=True,
        write=_FakeUseCase(error=InvalidFilePathError("x", reason="name_too_long")),
    )
    client = _client(app)
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "x" * 300},
        json={"content_base64": ""},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "name_too_long"


def test_write_platform_managed_key_is_422_naming_the_key() -> None:
    # A hand edit of server.properties that moves a platform-owned key is refused
    # and the response says which key is off limits (issue #2623).
    app = _app(
        member=True,
        allow=True,
        write=_FakeUseCase(error=PlatformManagedKeyError("server-port")),
    )
    client = _client(app)
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "server.properties"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "platform_managed_key"
    assert body["key"] == "server-port"


def test_write_under_a_file_parent_is_409() -> None:
    # A non-directory blocking a needed parent is the never-clobber 409 (issue #2433).
    app = _app(
        member=True,
        allow=True,
        write=_FakeUseCase(error=FileAlreadyExistsError("file/child")),
    )
    client = _client(app)
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "file/child"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "destination_exists"


# --- history / rollback ----------------------------------------------------


def test_history_lists_versions() -> None:
    app = _app(member=True, allow=True, history=_FakeUseCase(result=["v2", "v1"]))
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/history"), params={"path": "f"}
    )
    assert resp.status_code == 200
    assert resp.json()["versions"] == ["v2", "v1"]


def test_version_returns_base64_content() -> None:
    raw = bytes(range(256))  # non-UTF-8 bytes prove no encoding mangling
    app = _app(member=True, allow=True, version=_FakeUseCase(result=raw))
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/version"),
        params={"path": "f", "version_id": "missing"},
    )
    assert resp.status_code == 404


def test_version_traversal_is_422() -> None:
    app = _app(
        member=True, allow=True, version=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = _client(app)
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
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/version"),
        params={"path": "f", "version_id": "bad.id"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_version_id"


def test_rollback_success_is_204() -> None:
    use_case = _FakeUseCase()
    app = _app(member=True, allow=True, rollback=use_case)
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        params={"path": ".", "extract": "true"},
        files={"file": ("pack.zip", b"zip-bytes", "application/zip")},
    )
    assert resp.status_code == 204
    assert upload.calls[0]["extract"] is True


def test_upload_requires_file_edit_permission() -> None:
    app = _app(member=True, allow=False, upload=_FakeUpload())
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "level.dat"},
        headers=_bearer(),
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
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "world"},
        headers=_bearer(),
    )
    assert resp.status_code == 200
    assert resp.content == b"PKzip-tail"
    assert resp.headers["content-type"] == "application/zip"
    cd = resp.headers["content-disposition"]
    assert 'filename="world.zip"' in cd
    assert "filename*=UTF-8''world.zip" in cd


def test_download_requires_file_read_permission() -> None:
    app = _app(member=True, allow=False, download=_FakeDownload())
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "f"},
        headers=_bearer(),
    )
    assert resp.status_code == 403


def test_download_missing_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=ServerFileNotFoundError("x")),
    )
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "f"},
        headers=_bearer(),
    )
    assert resp.status_code == 404


def test_download_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=ServerFilesUnsettledError("x")),
    )
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "f"},
        headers=_bearer(),
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


# --- download Content-Disposition (RFC 6266 / 5987) ------------------------


def test_download_filename_with_quote_is_sanitized() -> None:
    # A file named evil".zip must not break out of the quoted-string and inject
    # extra Content-Disposition parameters; the quote is replaced in the ASCII
    # fallback and the real name is carried percent-encoded in filename*.
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=False))
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": 'evil".zip'},
        headers=_bearer(),
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
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "ワールド.zip"},
        headers=_bearer(),
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "f"},
        headers=_bearer(),
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
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "f"},
        headers=_bearer(),
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
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "../escape"},
        headers=_bearer(),
    )
    assert resp.status_code == 422
    assert recorder.events == []


# --- rename (issue #259) ---------------------------------------------------


def test_rename_is_204_and_passes_paths() -> None:
    rename = _FakeUseCase()
    app = _app(member=True, allow=True, rename=rename)
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "old.txt", "to": "new.txt"},
    )
    assert resp.status_code == 204
    assert rename.calls[0]["from_path"] == "old.txt"
    assert rename.calls[0]["to_path"] == "new.txt"


def test_rename_requires_file_edit_permission() -> None:
    app = _app(member=True, allow=False, rename=_FakeUseCase())
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "a", "to": "b"},
    )
    assert resp.status_code == 403


def test_rename_missing_source_is_404() -> None:
    app = _app(
        member=True, allow=True, rename=_FakeUseCase(error=ServerFileNotFoundError("x"))
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"), json={"from": "a", "to": "b"}
    )
    assert resp.status_code == 404


def test_rename_existing_destination_is_409() -> None:
    app = _app(
        member=True, allow=True, rename=_FakeUseCase(error=FileAlreadyExistsError("b"))
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"), json={"from": "a", "to": "b"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "destination_exists"


def test_rename_traversal_is_422() -> None:
    app = _app(
        member=True, allow=True, rename=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "a", "to": "../escape"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"


def test_rename_to_over_long_destination_surfaces_name_too_long() -> None:
    # An over-long rename destination is a modelled 422 name_too_long, not a bare
    # ENAMETOOLONG 500 — the route forwards exc.reason (issue #2433).
    app = _app(
        member=True,
        allow=True,
        rename=_FakeUseCase(error=InvalidFilePathError("x", reason="name_too_long")),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "a", "to": "x" * 300},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "name_too_long"


def test_rename_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        rename=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"), json={"from": "a", "to": "b"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_rename_success_records_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, rename=_FakeUseCase(), recorder=recorder)
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "old.txt"})
    assert resp.status_code == 204
    assert delete.calls[0]["rel_path"] == "old.txt"


def test_delete_requires_file_edit_permission() -> None:
    app = _app(member=True, allow=False, delete=_FakeUseCase())
    client = _client(app)
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 403


def test_delete_missing_is_404() -> None:
    app = _app(
        member=True, allow=True, delete=_FakeUseCase(error=ServerFileNotFoundError("x"))
    )
    client = _client(app)
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 404


def test_delete_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        delete=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = _client(app)
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_delete_success_records_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, delete=_FakeUseCase(), recorder=recorder)
    client = _client(app)
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.FILE_DELETE]
    assert recorder.events[0].outcome is Outcome.SUCCESS


# --- mkdir (issue #259) ----------------------------------------------------


def test_mkdir_is_204_and_passes_path() -> None:
    mkdir = _FakeUseCase()
    app = _app(member=True, allow=True, mkdir=mkdir)
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "plugins"}
    )
    assert resp.status_code == 204
    assert mkdir.calls[0]["rel_path"] == "plugins"


def test_mkdir_requires_file_edit_permission() -> None:
    app = _app(member=True, allow=False, mkdir=_FakeUseCase())
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "p"}
    )
    assert resp.status_code == 403


def test_mkdir_traversal_is_422() -> None:
    app = _app(
        member=True, allow=True, mkdir=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "../escape"}
    )
    assert resp.status_code == 422


def test_mkdir_over_long_name_surfaces_name_too_long() -> None:
    # An over-long directory name is a modelled 422 name_too_long, not a bare
    # ENAMETOOLONG 500 — the route forwards exc.reason (issue #2433).
    app = _app(
        member=True,
        allow=True,
        mkdir=_FakeUseCase(error=InvalidFilePathError("x", reason="name_too_long")),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "x" * 300}
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "name_too_long"


def test_mkdir_onto_a_file_is_409() -> None:
    # make_dir onto a name held by a file (or an ancestor that is a file) is the
    # never-clobber 409, not a raw EEXIST/ENOTDIR 500 (issue #2433).
    app = _app(
        member=True,
        allow=True,
        mkdir=_FakeUseCase(error=FileAlreadyExistsError("occupied")),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "occupied"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "destination_exists"


def test_mkdir_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        mkdir=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"), params={"path": "p"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_mkdir_success_records_audit() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, mkdir=_FakeUseCase(), recorder=recorder)
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/search"), json={"query": "x"})
    assert resp.status_code == 403


def test_search_invalid_by_is_422() -> None:
    app = _app(
        member=True, allow=True, search=_FakeUseCase(error=InvalidFilePathError("x"))
    )
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)
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

    app = _shared_app
    app.dependency_overrides.clear()
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
    client = _client(app)

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

    app = _shared_app
    app.dependency_overrides.clear()
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
    client = _client(app)

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
    client = _client(app)

    resp = client.put(
        _url(community, server),
        params={"path": "世界/レベル.dat"},
        json={"content_base64": ""},
    )
    assert resp.status_code == 204


# --- file download grants (issue #2352) ------------------------------------


def _mint(
    client: TestClient, community: uuid.UUID, server: uuid.UUID, path: str
) -> httpx2.Response:
    return client.post(
        _url(community, server, "/download-grant"),
        params={"path": path},
        headers=_bearer(),
    )


def _file_grant_url(
    community: uuid.UUID, server: uuid.UUID, path: str, *, subject: User
) -> str:
    """The download URL a grant minted for this (server, path) would produce."""

    issued = _tokens.issue_download_grant(
        subject.id, file_download_grant_resource(community, server, path)
    )
    return (
        f"{_url(community, server, '/download')}"
        f"?path={quote(path, safe='')}&grant={issued.token}"
    )


def test_non_member_gets_404_on_file_download_grant() -> None:
    app = _app(member=False, allow=True, download=_FakeDownload())
    client = _client(app)
    assert _mint(client, uuid.uuid4(), uuid.uuid4(), "world").status_code == 404


def test_member_without_permission_gets_403_on_file_download_grant() -> None:
    app = _app(member=True, allow=False, download=_FakeDownload())
    client = _client(app)
    assert _mint(client, uuid.uuid4(), uuid.uuid4(), "world").status_code == 403


def test_file_download_grant_for_a_missing_path_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=ServerFileNotFoundError("x")),
    )
    client = _client(app)
    assert _mint(client, uuid.uuid4(), uuid.uuid4(), "gone").status_code == 404


def test_file_download_grant_for_a_traversal_path_is_422() -> None:
    app = _app(
        member=True, allow=True, download=_FakeDownload(error=InvalidFilePathError("x"))
    )
    client = _client(app)
    resp = _mint(client, uuid.uuid4(), uuid.uuid4(), "../escape")
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"


def test_file_download_grant_for_a_symlink_path_surfaces_the_symlink_reason() -> None:
    # The mint is the operator-visible failure point on the download surface: since
    # issue #2352 the Web UI mints a grant BEFORE any download, so a symlink path is
    # refused here and the download is never reached. Collapsing the reason to
    # invalid_path made the browser say "Invalid path" for a path the read route
    # already answers "Symbolic links are not allowed." for -- the parity issue
    # #2432 exists to reach, on the one at-rest read surface it had missed.
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(
            error=InvalidFilePathError("x", reason="symlink_refused")
        ),
    )
    client = _client(app)
    resp = _mint(client, uuid.uuid4(), uuid.uuid4(), "alias/inner.txt")
    assert resp.status_code == 422
    assert resp.json()["reason"] == "symlink_refused"


def test_download_of_a_symlink_path_surfaces_the_symlink_reason() -> None:
    # The redemption side of the same surface: a grant minted before the link was
    # planted, or a direct Bearer download, still has to answer with the reason the
    # read route gives rather than a blanket invalid_path (issue #2432).
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(
            error=InvalidFilePathError("x", reason="symlink_refused")
        ),
    )
    client = _client(app)
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/download"),
        params={"path": "alias/inner.txt"},
        headers=_bearer(),
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "symlink_refused"


def test_file_download_grant_for_a_running_server_is_409_and_audits_denied() -> None:
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = _client(app)
    resp = _mint(client, uuid.uuid4(), uuid.uuid4(), "world")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"
    # The denied row the download would have recorded: minting first must not
    # delete denied-download visibility from the audit log.
    assert [e.operation for e in recorder.events] == [ops.FILE_DOWNLOAD]
    assert recorder.events[0].outcome is Outcome.DENIED


def test_file_download_grant_response_is_not_cached_and_reports_expiry() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    resp = _mint(client, community, server, "world/nether")
    assert resp.status_code == 200
    # A URL that carries a credential must never sit in a shared cache.
    assert resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["download_url"].startswith(
        f"{_url(community, server, '/download')}?path=world%2Fnether&grant="
    )
    assert body["expires_at"] == (
        _NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS)
    ).isoformat().replace("+00:00", "Z")


def test_minting_a_file_grant_records_no_audit_event() -> None:
    # Bytes leave the system at redemption, not at issuance (issue #2352).
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True, allow=True, download=_FakeDownload(is_dir=True), recorder=recorder
    )
    client = _client(app)
    assert _mint(client, uuid.uuid4(), uuid.uuid4(), "world").status_code == 200
    assert recorder.events == []


def test_minted_file_url_downloads_a_directory_zip_without_a_header() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(is_dir=True, zip_chunks=[b"PK", b"zip-tail"]),
        recorder=recorder,
    )
    client = _client(app)
    url = _mint(client, community, server, "world").json()["download_url"]

    resp = client.get(url)

    assert resp.status_code == 200
    assert resp.content == b"PKzip-tail"
    assert resp.headers["content-type"] == "application/zip"
    assert 'filename="world.zip"' in resp.headers["content-disposition"]
    # Exactly one audit row, at redemption, with the grant's subject as actor.
    assert [e.operation for e in recorder.events] == [ops.FILE_DOWNLOAD]
    assert recorder.events[0].actor_id == _user.id.value


def test_minted_file_url_downloads_a_single_file_without_a_header() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(is_dir=False, file_content=b"level-bytes"),
        recorder=recorder,
    )
    client = _client(app)
    url = _mint(client, community, server, "level.dat").json()["download_url"]

    resp = client.get(url)

    assert resp.status_code == 200
    assert resp.content == b"level-bytes"
    assert 'filename="level.dat"' in resp.headers["content-disposition"]
    assert [e.operation for e in recorder.events] == [ops.FILE_DOWNLOAD]
    assert recorder.events[0].actor_id == _user.id.value


def test_file_grant_redeemed_download_matches_the_bearer_response() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True, allow=True, download=_FakeDownload(is_dir=True, zip_chunks=[b"PK"])
    )
    client = _client(app)

    with_bearer = client.get(
        _url(community, server, "/download"),
        params={"path": "world"},
        headers=_bearer(),
    )
    with_grant = client.get(_file_grant_url(community, server, "world", subject=_user))

    assert with_grant.status_code == with_bearer.status_code == 200
    assert with_grant.content == with_bearer.content
    for header in ("content-type", "content-disposition"):
        assert with_grant.headers[header] == with_bearer.headers[header]


def test_file_grant_is_rejected_on_another_path() -> None:
    # The binding is exact string equality (fail-closed): a grant for `world`
    # redeems `world` and nothing else, not even another spelling of it.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    issued = _tokens.issue_download_grant(
        _user.id, file_download_grant_resource(community, server, "world")
    )

    other_path = client.get(
        _url(community, server, "/download"),
        params={"path": "secrets", "grant": issued.token},
    )
    traversal = client.get(
        _url(community, server, "/download"),
        params={"path": "world/../secrets", "grant": issued.token},
    )

    assert other_path.status_code == 401
    assert traversal.status_code == 401


def test_file_grant_is_rejected_under_another_server_or_community() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    issued = _tokens.issue_download_grant(
        _user.id, file_download_grant_resource(community, server, "world")
    )

    other_server = client.get(
        _url(community, uuid.uuid4(), "/download"),
        params={"path": "world", "grant": issued.token},
    )
    other_community = client.get(
        _url(uuid.uuid4(), server, "/download"),
        params={"path": "world", "grant": issued.token},
    )

    assert other_server.status_code == 401
    assert other_community.status_code == 401


def test_export_grant_is_rejected_at_the_file_download() -> None:
    # The resource prefixes separate the two surfaces (issue #2352).
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    issued = _tokens.issue_download_grant(
        _user.id, export_download_grant_resource(community, server)
    )

    resp = client.get(
        _url(community, server, "/download"),
        params={"path": "world", "grant": issued.token},
    )

    assert resp.status_code == 401


def test_file_grant_is_rejected_at_the_export_download() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    issued = _tokens.issue_download_grant(
        _user.id, file_download_grant_resource(community, server, "world")
    )

    resp = client.get(
        f"/api/communities/{community}/servers/{server}/export",
        params={"grant": issued.token},
    )

    assert resp.status_code == 401


def test_file_grant_is_rejected_after_its_ttl() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    url = _file_grant_url(community, server, "world", subject=_user)

    _clock.set(_NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS))

    assert client.get(url).status_code == 401


def test_file_grant_is_not_accepted_as_a_bearer_token() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    issued = _tokens.issue_download_grant(
        _user.id, file_download_grant_resource(community, server, "world")
    )

    resp = client.get(
        _url(community, server, "/download"),
        params={"path": "world"},
        headers={"Authorization": f"Bearer {issued.token}"},
    )

    assert resp.status_code == 401


def test_access_token_is_not_accepted_as_a_file_grant() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    access = _tokens.issue_access_token(_user.id)

    resp = client.get(
        _url(community, server, "/download"),
        params={"path": "world", "grant": access},
    )

    assert resp.status_code == 401


def test_file_grant_loses_to_a_permission_revoked_after_issuance() -> None:
    # The grant proves identity, never authority: authorization is decided afresh.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=False, download=_FakeDownload(is_dir=True))
    client = _client(app)

    resp = client.get(_file_grant_url(community, server, "world", subject=_user))

    assert resp.status_code == 403


def test_file_grant_loses_to_a_membership_removed_after_issuance() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=False, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)

    resp = client.get(_file_grant_url(community, server, "world", subject=_user))

    assert resp.status_code == 404


def test_file_grant_for_a_deactivated_subject_is_rejected() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        subject=make_user(active=False),
        download=_FakeDownload(is_dir=True),
    )
    client = _client(app)

    resp = client.get(_file_grant_url(community, server, "world", subject=_user))

    assert resp.status_code == 401


def test_file_download_without_any_credential_is_401() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)

    resp = client.get(_url(community, server, "/download"), params={"path": "world"})

    assert resp.status_code == 401


# --- the file download cookie (issue #2373) ---------------------------------
#
# The file download shares one mechanism with the backup and export downloads
# (``require_download_access`` + ``download_cookie``), and the property matrix is
# pinned once in test_backup_endpoints.py. What is per-route here is the Path
# scope — which cannot name the ``?path=`` this route is keyed by, so two paths on
# one server share a cookie slot — and that the retry of *this* URL resumes.


def _file_cookie_header(
    community: uuid.UUID, server: uuid.UUID, path: str
) -> dict[str, str]:
    value = _tokens.issue_download_cookie(
        _user.id, file_download_grant_resource(community, server, path)
    )
    return {"Cookie": f"{DOWNLOAD_COOKIE_NAME}={value}"}


def test_file_grant_redemption_sets_a_path_scoped_cookie() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)

    resp = client.get(_file_grant_url(community, server, "world", subject=_user))

    assert resp.status_code == 200
    cookie = next(
        h
        for h in resp.headers.get_list("set-cookie")
        if h.startswith(f"{DOWNLOAD_COOKIE_NAME}=")
    )
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    # The query string is not part of a cookie's Path, so the scope is the route.
    assert f"Path={_url(community, server, '/download')}" in cookie
    # RFC 6265 Section 3 leaves a Set-Cookie response cacheable, so without this
    # header a shared cache could replay the credential to a second client.
    assert resp.headers["cache-control"] == "no-store"


def test_expired_file_grant_is_retried_with_the_cookie() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    url = _file_grant_url(community, server, "world", subject=_user)

    _clock.set(_NOW + dt.timedelta(seconds=_GRANT_TTL_SECONDS))

    assert client.get(url).status_code == 401
    resumed = client.get(url, headers=_file_cookie_header(community, server, "world"))
    assert resumed.status_code == 200


def test_file_cookie_is_rejected_on_another_path() -> None:
    # The Path attribute cannot separate two downloads on one server, so the
    # signed resource claim is the whole boundary here: a cookie minted for
    # ``world`` opens nothing else, even at the identical URL path.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)

    resp = client.get(
        _url(community, server, "/download"),
        params={"path": "plugins"},
        headers=_file_cookie_header(community, server, "world"),
    )

    assert resp.status_code == 401


def test_file_cookie_is_rejected_under_another_server_or_community() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)
    headers = _file_cookie_header(community, server, "world")

    other_server = client.get(
        _url(community, uuid.uuid4(), "/download"),
        params={"path": "world"},
        headers=headers,
    )
    other_community = client.get(
        _url(uuid.uuid4(), server, "/download"),
        params={"path": "world"},
        headers=headers,
    )

    assert other_server.status_code == 401
    assert other_community.status_code == 401


# --- Cache-Control on the served download (issue #2491) ---------------------


@pytest.mark.parametrize(("path", "is_dir"), [("world", True), ("level.dat", False)])
def test_file_download_declares_no_store_under_every_credential(
    path: str, is_dir: bool
) -> None:
    # The header belongs to the response being a per-user body, not to the
    # credential that fetched it, and both branches -- the directory zip and the
    # single file -- are one such body. A cookie-authenticated request in
    # particular carries no Authorization, so RFC 9111 Section 3.5's default
    # protection from shared caches does not cover it.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(
            is_dir=is_dir, zip_chunks=[b"PK"], file_content=b"level-bytes"
        ),
    )
    client = _client(app)
    url = _url(community, server, "/download")

    with_bearer = client.get(url, params={"path": path}, headers=_bearer())
    with_cookie = client.get(
        url, params={"path": path}, headers=_file_cookie_header(community, server, path)
    )
    # Last, because redeeming a grant mints the cookie into the client's jar.
    with_grant = client.get(_file_grant_url(community, server, path, subject=_user))

    for resp in (with_bearer, with_cookie, with_grant):
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"


# --- the HEAD probe (issue #2383) ------------------------------------------
#
# A download client asks HEAD first, to learn what the transfer would be before
# starting it. Per credential and per branch, like the Cache-Control section
# above: the gate is what a future auth change could silently drop a route from.


@pytest.mark.parametrize(("path", "is_dir"), [("world", True), ("level.dat", False)])
def test_file_head_answers_the_gets_headers_under_every_credential(
    path: str, is_dir: bool
) -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(
            is_dir=is_dir, zip_chunks=[b"PK"], file_content=b"level-bytes"
        ),
    )
    client = _client(app)
    url = _url(community, server, "/download")
    cookie = _file_cookie_header(community, server, path)
    # Last, because redeeming a grant mints the cookie into the client's jar.
    grant_url = _file_grant_url(community, server, path, subject=_user)

    probes = [
        client.head(url, params={"path": path}, headers=_bearer()),
        client.head(url, params={"path": path}, headers=cookie),
        client.head(grant_url),
    ]
    served = client.get(url, params={"path": path}, headers=_bearer())

    for resp in probes:
        assert resp.status_code == served.status_code == 200
        # A HEAD carries the GET's headers and none of its bytes.
        assert resp.content == b""
        for name in ("content-type", "content-disposition", "cache-control"):
            assert resp.headers[name] == served.headers[name], name
        if is_dir:
            # The directory zip is built incrementally, so the GET declares no
            # length; a probe that answered "0" would tell the client it is
            # empty. Asserted as an absence, not as equality with the GET's own
            # absent header — that comparison is None == None and can never fail.
            assert "content-length" not in resp.headers
            assert "content-length" not in served.headers
        else:
            assert resp.headers["content-length"] == served.headers["content-length"]


@pytest.mark.parametrize(("path", "is_dir"), [("world", True), ("level.dat", False)])
def test_file_head_neither_opens_the_download_nor_records_one(
    path: str, is_dir: bool
) -> None:
    # Returning the right headers is easy; doing it without opening the bytes is
    # the point of the probe (issue #2383). And a probe is not a download, so it
    # records nothing: an audited HEAD would inflate the file:download count.
    community, server = uuid.uuid4(), uuid.uuid4()
    download = _FakeDownload(
        is_dir=is_dir, zip_chunks=[b"PK"], file_content=b"level-bytes"
    )
    recorder = RecordingAuditRecorder()
    app = _app(member=True, allow=True, download=download, recorder=recorder)
    client = _client(app)

    resp = client.head(
        _url(community, server, "/download"), params={"path": path}, headers=_bearer()
    )

    assert resp.status_code == 200
    # Only the cheap probes ran: never dir_zip, never file_stream.
    assert download.calls == (["is_dir"] if is_dir else ["is_dir", "file_size"])
    assert recorder.events == []


def test_file_head_of_a_running_server_is_409_and_records_nothing() -> None:
    # The GET records file:download DENIED here; the probe does not, for the same
    # reason it does not record a success — it is not a download attempt.
    community, server = uuid.uuid4(), uuid.uuid4()
    recorder = RecordingAuditRecorder()
    app = _app(
        member=True,
        allow=True,
        download=_FakeDownload(error=ServerFilesUnsettledError("x")),
        recorder=recorder,
    )
    client = _client(app)

    resp = client.head(
        _url(community, server, "/download"),
        params={"path": "world"},
        headers=_bearer(),
    )

    assert resp.status_code == 409
    assert recorder.events == []


def _download_credential(
    kind: str, community: uuid.UUID, server: uuid.UUID, path: str
) -> tuple[str, dict[str, str] | None, dict[str, str]]:
    """The download URL, query and headers for one of the three transports.

    The grant URL already carries the ``?path=`` it was bound to, so its query is
    ``None`` rather than an empty dict — httpx drops the URL's own parameters when
    a ``params`` mapping is passed.
    """

    url = _url(community, server, "/download")
    if kind == "bearer":
        return url, {"path": path}, _bearer()
    if kind == "cookie":
        return url, {"path": path}, _file_cookie_header(community, server, path)
    return _file_grant_url(community, server, path, subject=_user), None, {}


@pytest.mark.parametrize("kind", ["bearer", "cookie", "grant"])
@pytest.mark.parametrize(
    ("member", "allow", "error", "expected"),
    [
        (True, True, None, 200),
        (False, True, None, 404),
        (True, False, None, 403),
        (True, True, ServerFileNotFoundError("x"), 404),
        (True, True, InvalidFilePathError("x", reason="symlink_refused"), 422),
        (True, True, ServerFilesUnsettledError("x"), 409),
    ],
)
def test_file_head_is_answered_exactly_like_the_get(
    kind: str, member: bool, allow: bool, error: Exception | None, expected: int
) -> None:
    # A HEAD that skipped or weakened a check would be a security defect, not a
    # convenience gap. Every credential must be refused on the probe exactly
    # where it is refused on the download.
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(
        member=member, allow=allow, download=_FakeDownload(is_dir=True, error=error)
    )
    url, params, headers = _download_credential(kind, community, server, "world")

    probed = _client(app).head(url, params=params, headers=headers)
    served = _client(app).get(url, params=params, headers=headers)

    assert probed.status_code == served.status_code == expected


def test_file_head_without_credentials_is_401() -> None:
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(member=True, allow=True, download=_FakeDownload(is_dir=True))
    client = _client(app)

    resp = client.head(_url(community, server, "/download"), params={"path": "world"})

    assert resp.status_code == 401


# --- platform-managed keys: the sibling write routes (issue #2809) ---------
#
# The PUT is not the only route that reaches the root server.properties, so the
# 422 contract the Web UI reads (``platform_managed_key`` plus the offending
# ``key``) is pinned on every route that can now refuse for that reason.


def test_delete_platform_managed_key_is_422_naming_the_key() -> None:
    app = _app(
        member=True,
        allow=True,
        delete=_FakeUseCase(error=PlatformManagedKeyError("rcon.password")),
    )
    client = _client(app)
    resp = client.delete(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": "server.properties"}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "platform_managed_key"
    assert body["key"] == "rcon.password"


def test_rename_platform_managed_key_is_422_naming_the_key() -> None:
    app = _app(
        member=True,
        allow=True,
        rename=_FakeUseCase(error=PlatformManagedKeyError("rcon.password")),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "server.properties", "to": "server.properties.bak"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "platform_managed_key"
    assert body["key"] == "rcon.password"


def test_rename_oversized_properties_source_is_413() -> None:
    # The rename-onto-server.properties guard caps the source it compares, so the
    # route must answer 413 rather than let a FileTooLargeError escape as a 500.
    app = _app(
        member=True, allow=True, rename=_FakeUseCase(error=FileTooLargeError("x"))
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "huge.bin", "to": "server.properties"},
    )
    assert resp.status_code == 413
    assert resp.json()["reason"] == "file_too_large"


def test_upload_platform_managed_key_is_422_naming_the_key() -> None:
    app = _app(
        member=True,
        allow=True,
        upload=_FakeUpload(error=PlatformManagedKeyError("server-port")),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        params={"path": "."},
        files={"file": ("server.properties", b"x", "text/plain")},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "platform_managed_key"
    assert body["key"] == "server-port"


def test_rollback_platform_managed_key_is_422_naming_the_key() -> None:
    app = _app(
        member=True,
        allow=True,
        rollback=_FakeUseCase(error=PlatformManagedKeyError("server-port")),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rollback"),
        params={"path": "server.properties"},
        json={"version_id": "v1"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "platform_managed_key"
    assert body["key"] == "server-port"


def test_delete_symlink_refused_renders_invalid_path() -> None:
    # The DELETE route deliberately does NOT forward exc.reason (unlike the PUT
    # and rename routes): a refused path is one 422 invalid_path whatever refused
    # it. So a symlink standing in for the root server.properties — which the
    # platform-key guard now refuses rather than unlinks (issue #2809) — renders
    # invalid_path, not symlink_refused. Pinned so the choice is deliberate.
    app = _app(
        member=True,
        allow=True,
        delete=_FakeUseCase(error=InvalidFilePathError("x", reason="symlink_refused")),
    )
    client = _client(app)
    resp = client.delete(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": "server.properties"}
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_path"


def test_rollback_oversized_version_is_413() -> None:
    # Centralizing the cap inside the shared guard made FileTooLargeError
    # reachable from rollback: it reads the retained version to compare it, and a
    # legacy oversized version of the root server.properties predates the cap
    # every write door now enforces. Without this mapping it escapes as a 500.
    app = _app(
        member=True, allow=True, rollback=_FakeUseCase(error=FileTooLargeError("x"))
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rollback"),
        params={"path": "server.properties"},
        json={"version_id": "v1"},
    )
    assert resp.status_code == 413
    assert resp.json()["reason"] == "file_too_large"


# --- no directory at the root server.properties path (issue #2812) ---------
#
# The two routes this change closes answer 422 with the
# ``platform_managed_path`` reason, pinned per route so the Web UI's switch on
# ``reason`` sees the same contract from both.


def test_mkdir_at_root_properties_path_is_422_platform_managed_path() -> None:
    app = _app(
        member=True,
        allow=True,
        mkdir=_FakeUseCase(
            error=InvalidFilePathError(
                "server.properties", reason="platform_managed_path"
            )
        ),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/directories"),
        params={"path": "server.properties"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "platform_managed_path"


def test_rename_dir_onto_root_properties_path_is_422_platform_managed_path() -> None:
    app = _app(
        member=True,
        allow=True,
        rename=_FakeUseCase(
            error=InvalidFilePathError(
                "server.properties", reason="platform_managed_path"
            )
        ),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "staged", "to": "server.properties"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "platform_managed_path"


# --- the file-side doors to the same directory (issue #2846) ---------------
#
# The write / upload / file-rename family reaches that directory too, by
# creating the destination's parents. Same 422 ``platform_managed_path``, so the
# Web UI's switch on ``reason`` sees ONE contract across every door.


def test_write_under_root_properties_path_is_422_platform_managed_path() -> None:
    app = _app(
        member=True,
        allow=True,
        write=_FakeUseCase(
            error=InvalidFilePathError(
                "server.properties", reason="platform_managed_path"
            )
        ),
    )
    client = _client(app)
    resp = client.put(
        _url(uuid.uuid4(), uuid.uuid4()),
        params={"path": "server.properties/notes.txt"},
        json={"content_base64": base64.b64encode(b"x").decode()},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "platform_managed_path"


def test_rename_file_under_root_properties_path_is_422_platform_managed_path() -> None:
    app = _app(
        member=True,
        allow=True,
        rename=_FakeUseCase(
            error=InvalidFilePathError(
                "server.properties", reason="platform_managed_path"
            )
        ),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/rename"),
        json={"from": "ops.json", "to": "server.properties/ops.json"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "platform_managed_path"


def test_upload_under_root_properties_path_is_422_platform_managed_path() -> None:
    app = _app(
        member=True,
        allow=True,
        upload=_FakeUpload(
            error=InvalidFilePathError(
                "server.properties", reason="platform_managed_path"
            )
        ),
    )
    client = _client(app)
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/upload"),
        params={"path": "server.properties"},
        files={"file": ("notes.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "platform_managed_path"
