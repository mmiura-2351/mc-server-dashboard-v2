"""Endpoint tests for the lifecycle routes (start/stop/restart/command).

In-process via FastAPI's TestClient with the use cases and authorization Ports
faked (NFR-TEST-1, no DB / gRPC). Verifies the two-layer gate per route
(non-member 404, member-without-permission 403), the domain-error -> HTTP-code
mapping (invalid transition 409, no eligible worker / worker unavailable 503,
command failure 409, not-running 409), and the success serialisation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
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
    get_membership_visibility,
    get_permission_checker,
    get_restart_server,
    get_send_server_command,
    get_start_server,
    get_stop_server,
)
from mc_server_dashboard_api.servers.domain.control_plane import WorkerUnavailableError
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    InvalidLifecycleTransitionError,
    NoEligibleWorkerError,
    ServerNotFoundError,
    ServerNotRunningError,
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

    async def __call__(self, **kwargs: object) -> object:
        if self._error is not None:
            raise self._error
        return self._result


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _server(community_id: uuid.UUID) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=ServersCommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _app(
    *,
    member: bool,
    allow: bool,
    start: _FakeUseCase | None = None,
    stop: _FakeUseCase | None = None,
    restart: _FakeUseCase | None = None,
    command: _FakeUseCase | None = None,
) -> object:
    from tests.identity.fakes import make_user

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if start is not None:
        app.dependency_overrides[get_start_server] = lambda: start
    if stop is not None:
        app.dependency_overrides[get_stop_server] = lambda: stop
    if restart is not None:
        app.dependency_overrides[get_restart_server] = lambda: restart
    if command is not None:
        app.dependency_overrides[get_send_server_command] = lambda: command
    return app


def _url(community: uuid.UUID, server: uuid.UUID, action: str) -> str:
    return f"/communities/{community}/servers/{server}/{action}"


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_start() -> None:
    app = _app(member=False, allow=True, start=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_stop() -> None:
    app = _app(member=True, allow=False, stop=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "stop"))
    assert resp.status_code == 403


def test_member_without_permission_gets_403_on_command() -> None:
    app = _app(member=True, allow=False, command=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "command"), json={"line": "list"}
    )
    assert resp.status_code == 403


# --- success ---------------------------------------------------------------


def test_start_success_returns_server() -> None:
    community = uuid.uuid4()
    app = _app(member=True, allow=True, start=_FakeUseCase(result=_server(community)))
    client = next(_client(app))
    resp = client.post(_url(community, uuid.uuid4(), "start"))
    assert resp.status_code == 200
    assert resp.json()["desired_state"] == "running"


def test_command_success_returns_output() -> None:
    app = _app(member=True, allow=True, command=_FakeUseCase(result="players: 3"))
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "command"), json={"line": "list"}
    )
    assert resp.status_code == 200
    assert resp.json()["output"] == "players: 3"


# --- error mapping ---------------------------------------------------------


def test_start_invalid_transition_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        start=_FakeUseCase(error=InvalidLifecycleTransitionError("x")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "invalid_transition"


def test_start_no_eligible_worker_is_503() -> None:
    app = _app(
        member=True, allow=True, start=_FakeUseCase(error=NoEligibleWorkerError("x"))
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "no_eligible_worker"


def test_start_worker_unavailable_is_503() -> None:
    app = _app(
        member=True, allow=True, start=_FakeUseCase(error=WorkerUnavailableError("x"))
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "worker_unavailable"


def test_start_command_failure_is_409() -> None:
    app = _app(
        member=True, allow=True, start=_FakeUseCase(error=CommandDispatchError("x"))
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "start"))
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "command_failed"


def test_stop_missing_server_is_404() -> None:
    app = _app(
        member=True, allow=True, stop=_FakeUseCase(error=ServerNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "stop"))
    assert resp.status_code == 404


def test_command_not_running_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        command=_FakeUseCase(error=ServerNotRunningError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "command"), json={"line": "list"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "server_not_running"
