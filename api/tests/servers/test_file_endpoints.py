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

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
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
    get_current_user,
    get_list_dir,
    get_list_file_versions,
    get_membership_visibility,
    get_permission_checker,
    get_read_file,
    get_rollback_file,
    get_write_file,
)
from mc_server_dashboard_api.servers.domain.control_plane import WorkerUnavailableError
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidFilePathError,
    ServerFileNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileEntry
from tests.community.fakes import FakeAuthzUnitOfWork
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


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(
    *,
    member: bool,
    allow: bool,
    read: _FakeUseCase | None = None,
    list_: _FakeUseCase | None = None,
    write: _FakeUseCase | None = None,
    history: _FakeUseCase | None = None,
    rollback: _FakeUseCase | None = None,
) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if read is not None:
        app.dependency_overrides[get_read_file] = lambda: read
    if list_ is not None:
        app.dependency_overrides[get_list_dir] = lambda: list_
    if write is not None:
        app.dependency_overrides[get_write_file] = lambda: write
    if history is not None:
        app.dependency_overrides[get_list_file_versions] = lambda: history
    if rollback is not None:
        app.dependency_overrides[get_rollback_file] = lambda: rollback
    return app


def _url(community: uuid.UUID, server: uuid.UUID, suffix: str = "") -> str:
    return f"/communities/{community}/servers/{server}/files{suffix}"


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
    use_case = _FakeUseCase(result=[FileEntry(name="world", is_dir=True, size=0)])
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": ".", "list": "true"}
    )
    assert resp.status_code == 200
    assert resp.json()["entries"] == [{"name": "world", "is_dir": True, "size": 0}]


def test_list_disconnected_worker_is_503() -> None:
    app = _app(
        member=True, allow=True, list_=_FakeUseCase(error=WorkerUnavailableError("x"))
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4()), params={"path": ".", "list": "true"}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "worker_unavailable"


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
    assert resp.json()["detail"]["reason"] == "server_unsettled"


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
    assert resp.json()["detail"]["reason"] == "invalid_base64"


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
    assert resp.json()["detail"]["reason"] == "invalid_path"


def test_read_transitional_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        read=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "server_unsettled"


def test_read_disconnected_worker_is_503() -> None:
    app = _app(
        member=True, allow=True, read=_FakeUseCase(error=WorkerUnavailableError("x"))
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()), params={"path": "f"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "worker_unavailable"


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
    assert resp.json()["detail"]["reason"] == "file_too_large"


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
    assert resp.json()["detail"]["reason"] == "invalid_path"


# --- history / rollback ----------------------------------------------------


def test_history_lists_versions() -> None:
    app = _app(member=True, allow=True, history=_FakeUseCase(result=["v2", "v1"]))
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/history"), params={"path": "f"}
    )
    assert resp.status_code == 200
    assert resp.json()["versions"] == ["v2", "v1"]


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
    assert resp.json()["detail"]["reason"] == "server_not_stopped"


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
