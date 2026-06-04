"""Endpoint tests for the servers router (Section 6.5).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate per route: non-member -> 404, member-without-permission ->
  403, authorized member -> 2xx;
- domain-error -> HTTP-code mapping (unknown type/backend 422, backend-immutable
  409, update/delete-while-running 409, cross-community / missing server 404);
- per-resource gating with the *real* role+grant checker: a grant on server X
  opens exactly X (server Y stays 403), and a server in community A is invisible
  through community B's routes (cross-community isolation).
"""

from __future__ import annotations

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
    get_create_server,
    get_current_user,
    get_delete_server,
    get_list_servers,
    get_membership_visibility,
    get_permission_checker,
    get_read_server,
    get_update_server,
)
from mc_server_dashboard_api.servers.application.manage_server import ReadServer
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ExecutionBackendImmutableError,
    InvalidBackupScheduleError,
    InvalidSnapshotIntervalError,
    ServerNotFoundError,
    ServerNotStoppedError,
    UnknownExecutionBackendError,
    UnknownServerTypeError,
    UnsupportedEditionError,
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
from mc_server_dashboard_api.servers.domain.version_validator import (
    CatalogUnavailableError,
    UnsupportedServerTypeError,
)
from mc_server_dashboard_api.servers.domain.version_validator import (
    UnknownVersionError as CatalogUnknownVersionError,
)
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


def _server_entity(
    *,
    community_id: uuid.UUID,
    server_id: uuid.UUID | None = None,
    name: str = "survival",
    desired: DesiredState = DesiredState.STOPPED,
    observed: ObservedState = ObservedState.STOPPED,
) -> Server:
    return Server(
        id=ServerId(server_id or uuid.uuid4()),
        community_id=ServersCommunityId(community_id),
        name=ServerName(name),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={"motd": "hi"},
        desired_state=desired,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _app(
    *,
    member: bool,
    allow: bool,
    create: _FakeUseCase | None = None,
    read: _FakeUseCase | None = None,
    list_: _FakeUseCase | None = None,
    update: _FakeUseCase | None = None,
    delete: _FakeUseCase | None = None,
) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if create is not None:
        app.dependency_overrides[get_create_server] = lambda: create
    if read is not None:
        app.dependency_overrides[get_read_server] = lambda: read
    if list_ is not None:
        app.dependency_overrides[get_list_servers] = lambda: list_
    if update is not None:
        app.dependency_overrides[get_update_server] = lambda: update
    if delete is not None:
        app.dependency_overrides[get_delete_server] = lambda: delete
    return app


def _create_body() -> dict[str, object]:
    return {
        "name": "survival",
        "mc_edition": "java",
        "mc_version": "1.21.1",
        "server_type": "vanilla",
        "execution_backend": "host_process",
        "config": {"motd": "hi"},
    }


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_create() -> None:
    app = _app(member=False, allow=True, create=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(f"/communities/{uuid.uuid4()}/servers", json=_create_body())
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_create() -> None:
    app = _app(member=True, allow=False, create=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(f"/communities/{uuid.uuid4()}/servers", json=_create_body())
    assert resp.status_code == 403


def test_authorized_member_creates_server() -> None:
    community = uuid.uuid4()
    server = _server_entity(community_id=community)
    use_case = _FakeUseCase(result=server)
    app = _app(member=True, allow=True, create=use_case)
    client = next(_client(app))
    resp = client.post(f"/communities/{community}/servers", json=_create_body())
    assert resp.status_code == 201
    assert resp.json()["desired_state"] == "stopped"
    assert resp.json()["execution_backend"] == "host_process"


def test_non_member_gets_404_on_read() -> None:
    app = _app(member=False, allow=True, read=_FakeUseCase())
    client = next(_client(app))
    resp = client.get(f"/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_delete() -> None:
    app = _app(member=True, allow=False, delete=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}")
    assert resp.status_code == 403


# --- domain-error mapping --------------------------------------------------


def test_create_unknown_server_type_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=UnknownServerTypeError("x")),
    )
    client = next(_client(app))
    resp = client.post(f"/communities/{uuid.uuid4()}/servers", json=_create_body())
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_server_type"


def test_create_unknown_backend_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=UnknownExecutionBackendError("x")),
    )
    client = next(_client(app))
    resp = client.post(f"/communities/{uuid.uuid4()}/servers", json=_create_body())
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_execution_backend"


def test_create_unsupported_type_forge_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=UnsupportedServerTypeError("forge")),
    )
    client = next(_client(app))
    resp = client.post(f"/communities/{uuid.uuid4()}/servers", json=_create_body())
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "unsupported_server_type"


def test_create_unknown_version_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=CatalogUnknownVersionError("vanilla 9.9.9")),
    )
    client = next(_client(app))
    resp = client.post(f"/communities/{uuid.uuid4()}/servers", json=_create_body())
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "unknown_version"


def test_create_unsupported_edition_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=UnsupportedEditionError("bedrock")),
    )
    client = next(_client(app))
    resp = client.post(f"/communities/{uuid.uuid4()}/servers", json=_create_body())
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "unsupported_edition"


def test_create_catalog_unavailable_is_503() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=CatalogUnavailableError("source down")),
    )
    client = next(_client(app))
    resp = client.post(f"/communities/{uuid.uuid4()}/servers", json=_create_body())
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "catalog_unavailable"


def test_update_backend_immutable_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        update=_FakeUseCase(error=ExecutionBackendImmutableError("x")),
    )
    client = next(_client(app))
    resp = client.patch(
        f"/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}",
        json={"execution_backend": "container"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "execution_backend_immutable"


def test_update_while_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        update=_FakeUseCase(error=ServerNotStoppedError("x")),
    )
    client = next(_client(app))
    resp = client.patch(
        f"/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}",
        json={"name": "creative"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "server_not_stopped"


def test_update_snapshot_interval_below_floor_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        update=_FakeUseCase(error=InvalidSnapshotIntervalError("60")),
    )
    client = next(_client(app))
    resp = client.patch(
        f"/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}",
        json={"config": {"snapshot_interval_seconds": 60}},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_snapshot_interval"


def test_update_backup_interval_invalid_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        update=_FakeUseCase(error=InvalidBackupScheduleError("0")),
    )
    client = next(_client(app))
    resp = client.patch(
        f"/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}",
        json={"config": {"backup_interval_hours": 0}},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_backup_schedule"


def test_delete_while_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        delete=_FakeUseCase(error=ServerNotStoppedError("x")),
    )
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "server_not_stopped"


def test_read_missing_server_is_404() -> None:
    app = _app(
        member=True, allow=True, read=_FakeUseCase(error=ServerNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.get(f"/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_success_is_204() -> None:
    app = _app(member=True, allow=True, delete=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}")
    assert resp.status_code == 204


# --- per-resource grant + cross-community isolation (real checker) ----------


def _real_authz_app(
    *,
    user_id: uuid.UUID,
    authz_uow: FakeAuthzUnitOfWork,
    read_uow: FakeUnitOfWork,
) -> object:
    """App wired with the real role+grant checker and a real ReadServer use case."""

    app = create_app()
    user = make_user()
    user.id = type(user.id)(user_id)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: (
        RepositoryMembershipVisibility(authz_uow)
    )
    app.dependency_overrides[get_permission_checker] = lambda: (
        RoleGrantPermissionChecker(authz_uow)
    )
    app.dependency_overrides[get_read_server] = lambda: ReadServer(uow=read_uow)
    return app


def test_grant_on_one_server_opens_exactly_that_server() -> None:
    user_id = uuid.uuid4()
    community = uuid.uuid4()
    server_x = uuid.uuid4()
    server_y = uuid.uuid4()

    authz_uow = FakeAuthzUnitOfWork()
    user = UserId(user_id)
    com = CommunityId(community)
    # Member with no role permissions, but a per-resource grant on server X only.
    authz_uow.add_role(user, com, set())
    authz_uow.add_grant(user, com, "server", server_x, {Permission("server:read")})

    read_uow = FakeUnitOfWork()
    read_uow.servers.seed(_server_entity(community_id=community, server_id=server_x))
    read_uow.servers.seed(
        _server_entity(community_id=community, server_id=server_y, name="other")
    )

    app = _real_authz_app(user_id=user_id, authz_uow=authz_uow, read_uow=read_uow)
    client = next(_client(app))

    opened = client.get(f"/communities/{community}/servers/{server_x}")
    assert opened.status_code == 200
    assert opened.json()["id"] == str(server_x)

    blocked = client.get(f"/communities/{community}/servers/{server_y}")
    assert blocked.status_code == 403


def test_server_in_community_a_is_invisible_through_community_b() -> None:
    user_id = uuid.uuid4()
    community_a = uuid.uuid4()
    community_b = uuid.uuid4()
    server_in_a = uuid.uuid4()

    authz_uow = FakeAuthzUnitOfWork()
    user = UserId(user_id)
    # The user is a member of B with full server:read there, but not of A.
    authz_uow.add_role(user, CommunityId(community_b), {Permission("server:read")})

    read_uow = FakeUnitOfWork()
    read_uow.servers.seed(
        _server_entity(community_id=community_a, server_id=server_in_a)
    )

    app = _real_authz_app(user_id=user_id, authz_uow=authz_uow, read_uow=read_uow)
    client = next(_client(app))

    # Through B's route (the user is a member there) the A server does not exist.
    resp = client.get(f"/communities/{community_b}/servers/{server_in_a}")
    assert resp.status_code == 404

    # Through A's route the user is a non-member -> 404 (no existence signal).
    resp = client.get(f"/communities/{community_a}/servers/{server_in_a}")
    assert resp.status_code == 404
