"""Representative recording coverage (FR-AUD-1).

Proves the recorder is invoked from the routes with the right operation code and
outcome for a representative sample across contexts: auth (login success and
failure), community provisioning, server create, and worker drain. The recorder
is faked, so this asserts the edge wiring, not persistence (covered separately).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    CommunityName,
    Permission,
    ResourceRef,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_create_server,
    get_current_user,
    get_login,
    get_membership_visibility,
    get_permission_checker,
    get_provision_community,
    get_set_worker_drain,
)
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.errors import InvalidCredentialsError
from mc_server_dashboard_api.servers.domain.entities import Server
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
_COMMUNITY = uuid.uuid4()


class _FakeVisibility(MembershipVisibility):
    async def is_member(self, *, user_id: UserId, community_id: CommunityId) -> bool:
        return True


class _FakeChecker(PermissionChecker):
    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        return True


class _FakeUseCase:
    def __init__(self, *, result: object = None, error: Exception | None = None):
        self._result = result
        self._error = error

    async def __call__(self, **kwargs: object) -> object:
        if self._error is not None:
            raise self._error
        return self._result


class _FakeSyncUseCase:
    def __init__(self, *, result: bool) -> None:
        self._result = result

    def __call__(self, **kwargs: object) -> bool:
        return self._result


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _base_app(recorder: RecordingAuditRecorder, *, platform_admin: bool = False):  # type: ignore[no-untyped-def]
    app = create_app()
    user = make_user()
    user.is_platform_admin = platform_admin
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_audit_recorder] = lambda: recorder
    app.dependency_overrides[get_membership_visibility] = _FakeVisibility
    app.dependency_overrides[get_permission_checker] = _FakeChecker
    return app, user


def test_login_success_records_success() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    app.dependency_overrides[get_login] = lambda: _FakeUseCase(
        result=TokenPair(access_token="a", refresh_token="r")
    )
    client = next(_client(app))

    resp = client.post("/auth/login", json={"username": "alice", "password": "x"})

    assert resp.status_code == 200
    assert len(recorder.events) == 1
    assert recorder.events[0].operation == ops.AUTH_LOGIN
    assert recorder.events[0].outcome is Outcome.SUCCESS


def test_login_failure_records_denied() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    app.dependency_overrides[get_login] = lambda: _FakeUseCase(
        error=InvalidCredentialsError()
    )
    client = next(_client(app))

    resp = client.post("/auth/login", json={"username": "alice", "password": "x"})

    assert resp.status_code == 401
    assert len(recorder.events) == 1
    assert recorder.events[0].operation == ops.AUTH_LOGIN
    assert recorder.events[0].outcome is Outcome.DENIED


def test_provision_community_records_success() -> None:
    recorder = RecordingAuditRecorder()
    app, user = _base_app(recorder, platform_admin=True)
    community = Community(
        id=CommunityId.new(),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    app.dependency_overrides[get_provision_community] = lambda: _FakeUseCase(
        result=community
    )
    client = next(_client(app))

    resp = client.post(
        "/communities",
        json={"name": "guild", "owner_user_id": str(uuid.uuid4())},
    )

    assert resp.status_code == 201
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.operation == ops.COMMUNITY_PROVISION
    assert event.outcome is Outcome.SUCCESS
    assert event.actor_id == user.id.value
    assert event.community_id == community.id.value
    assert event.target_id == community.id.value


def test_create_server_records_success() -> None:
    recorder = RecordingAuditRecorder()
    app, user = _base_app(recorder)
    server = Server(
        id=ServerId.new(),
        community_id=ServersCommunityId(_COMMUNITY),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.20.4",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    app.dependency_overrides[get_create_server] = lambda: _FakeUseCase(result=server)
    client = next(_client(app))

    resp = client.post(
        f"/communities/{_COMMUNITY}/servers",
        json={
            "name": "survival",
            "mc_edition": "java",
            "mc_version": "1.20.4",
            "server_type": "vanilla",
            "execution_backend": "host_process",
        },
    )

    assert resp.status_code == 201
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.operation == ops.SERVER_CREATE
    assert event.outcome is Outcome.SUCCESS
    assert event.actor_id == user.id.value
    assert event.community_id == _COMMUNITY
    assert event.target_id == server.id.value


def test_set_worker_drain_records_success() -> None:
    recorder = RecordingAuditRecorder()
    app, user = _base_app(recorder, platform_admin=True)
    app.dependency_overrides[get_set_worker_drain] = lambda: _FakeSyncUseCase(
        result=True
    )
    client = next(_client(app))

    resp = client.put("/workers/worker-1/drain")

    assert resp.status_code == 204
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.operation == ops.WORKER_DRAIN_SET
    assert event.outcome is Outcome.SUCCESS
    assert event.actor_id == user.id.value


def test_set_worker_drain_unknown_worker_records_nothing() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder, platform_admin=True)
    app.dependency_overrides[get_set_worker_drain] = lambda: _FakeSyncUseCase(
        result=False
    )
    client = next(_client(app))

    resp = client.put("/workers/ghost/drain")

    assert resp.status_code == 404
    assert recorder.events == []
