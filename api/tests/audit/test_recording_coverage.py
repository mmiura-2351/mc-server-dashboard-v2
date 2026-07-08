"""Representative recording coverage (FR-AUD-1).

Proves the recorder is invoked from the routes with the right operation code and
outcome for a representative sample across contexts: auth (login success/failure,
refresh success, refresh-reuse denial, session-restore success), community
provisioning, server create, a
failed privileged server op (DENIED/ERROR), and worker drain. The recorder is
faked, so this asserts the edge wiring, not persistence (covered separately).
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
    get_refresh_session,
    get_restore_session,
    get_set_worker_drain,
    get_start_server,
)
from mc_server_dashboard_api.identity.application.login import LoginResult
from mc_server_dashboard_api.identity.application.restore_session import RestoreResult
from mc_server_dashboard_api.identity.application.token_pair import TokenPair
from mc_server_dashboard_api.identity.domain.errors import (
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    RefreshTokenReuseError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    LifecycleTransitionConflictError,
    NoEligibleWorkerError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    DesiredState,
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


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _base_app(recorder: RecordingAuditRecorder, *, platform_admin: bool = False):  # type: ignore[no-untyped-def]
    app = _shared_app
    app.dependency_overrides.clear()
    user = make_user()
    user.is_platform_admin = platform_admin
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_audit_recorder] = lambda: recorder
    app.dependency_overrides[get_membership_visibility] = _FakeVisibility
    app.dependency_overrides[get_permission_checker] = _FakeChecker
    return app, user


def test_login_success_records_success_with_actor() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    actor = uuid.uuid4()
    app.dependency_overrides[get_login] = lambda: _FakeUseCase(
        result=LoginResult(
            pair=TokenPair(access_token="a", refresh_token="r"), user_id=actor
        )
    )
    client = next(_client(app))

    resp = client.post("/api/auth/login", json={"username": "alice", "password": "x"})

    assert resp.status_code == 200
    assert len(recorder.events) == 1
    assert recorder.events[0].operation == ops.AUTH_LOGIN
    assert recorder.events[0].outcome is Outcome.SUCCESS
    # Success is now actor-attributable (FR-AUD-1).
    assert recorder.events[0].actor_id == actor


def test_login_failure_records_denied_without_actor() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    app.dependency_overrides[get_login] = lambda: _FakeUseCase(
        error=InvalidCredentialsError()
    )
    client = next(_client(app))

    resp = client.post("/api/auth/login", json={"username": "alice", "password": "x"})

    assert resp.status_code == 401
    assert len(recorder.events) == 1
    assert recorder.events[0].operation == ops.AUTH_LOGIN
    assert recorder.events[0].outcome is Outcome.DENIED
    # Failure stays unattributed (enumeration defence, SECURITY.md Section 2).
    assert recorder.events[0].actor_id is None


def test_refresh_success_records_success() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    app.dependency_overrides[get_refresh_session] = lambda: _FakeUseCase(
        result=TokenPair(access_token="a2", refresh_token="r2")
    )
    client = next(_client(app))

    resp = client.post("/api/auth/refresh", json={"refresh_token": "r1"})

    assert resp.status_code == 200
    assert len(recorder.events) == 1
    assert recorder.events[0].operation == ops.AUTH_REFRESH
    assert recorder.events[0].outcome is Outcome.SUCCESS


def test_refresh_reuse_records_denied_with_actor() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    affected = uuid.uuid4()
    app.dependency_overrides[get_refresh_session] = lambda: _FakeUseCase(
        error=RefreshTokenReuseError(affected)
    )
    client = next(_client(app))

    resp = client.post("/api/auth/refresh", json={"refresh_token": "reused"})

    assert resp.status_code == 401
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.operation == ops.AUTH_REFRESH_REUSE
    assert event.outcome is Outcome.DENIED
    assert event.actor_id == affected
    assert event.target_id == affected


def test_refresh_invalid_token_records_nothing() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    app.dependency_overrides[get_refresh_session] = lambda: _FakeUseCase(
        error=InvalidRefreshTokenError()
    )
    client = next(_client(app))

    resp = client.post("/api/auth/refresh", json={"refresh_token": "stale"})

    assert resp.status_code == 401
    # A plain bad/expired token is not a security event: no row (proportionate).
    assert recorder.events == []


def test_session_restore_success_records_success_with_actor() -> None:
    # Restore no longer trips an incidental theft signal (it never rotates), so a
    # SUCCESS row is the explicit replacement: it surfaces session-restore activity
    # per family for operators (issue #530).
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    actor = uuid.uuid4()
    app.dependency_overrides[get_restore_session] = lambda: _FakeUseCase(
        result=RestoreResult(access_token="a3", user_id=actor)
    )
    client = next(_client(app))
    client.cookies.set("mcd_refresh", "live-cookie")

    resp = client.post("/api/auth/session")

    assert resp.status_code == 200
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.operation == ops.AUTH_SESSION_RESTORE
    assert event.outcome is Outcome.SUCCESS
    assert event.actor_id == actor
    assert event.target_type == ops.TARGET_USER
    assert event.target_id == actor


def test_session_restore_invalid_token_records_nothing() -> None:
    # A missing/invalid cookie stays a silent 401 with no row — same proportionate
    # posture as a plain bad token on /refresh (issue #530).
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    app.dependency_overrides[get_restore_session] = lambda: _FakeUseCase(
        error=InvalidRefreshTokenError()
    )
    client = next(_client(app))
    client.cookies.set("mcd_refresh", "stale-cookie")

    resp = client.post("/api/auth/session")

    assert resp.status_code == 401
    assert recorder.events == []


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
        "/api/communities",
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
        f"/api/communities/{_COMMUNITY}/servers",
        json={
            "name": "survival",
            "mc_edition": "java",
            "mc_version": "1.20.4",
            "server_type": "vanilla",
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


def test_start_server_transition_conflict_records_denied() -> None:
    recorder = RecordingAuditRecorder()
    app, user = _base_app(recorder)
    app.dependency_overrides[get_start_server] = lambda: _FakeUseCase(
        error=LifecycleTransitionConflictError()
    )
    client = next(_client(app))

    resp = client.post(f"/api/communities/{_COMMUNITY}/servers/{uuid.uuid4()}/start")

    assert resp.status_code == 409
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.operation == ops.SERVER_START
    # A refused state transition is a DENIED outcome (issue #131).
    assert event.outcome is Outcome.DENIED
    assert event.actor_id == user.id.value
    assert event.community_id == _COMMUNITY


def test_start_server_no_eligible_worker_records_error() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder)
    app.dependency_overrides[get_start_server] = lambda: _FakeUseCase(
        error=NoEligibleWorkerError()
    )
    client = next(_client(app))

    resp = client.post(f"/api/communities/{_COMMUNITY}/servers/{uuid.uuid4()}/start")

    assert resp.status_code == 503
    assert len(recorder.events) == 1
    # A transient fleet failure is an ERROR outcome (issue #131).
    assert recorder.events[0].outcome is Outcome.ERROR


def test_set_worker_drain_records_success() -> None:
    recorder = RecordingAuditRecorder()
    app, user = _base_app(recorder, platform_admin=True)
    app.dependency_overrides[get_set_worker_drain] = lambda: _FakeUseCase(result=0)
    client = next(_client(app))

    resp = client.put("/api/workers/worker-1/drain")

    assert resp.status_code == 200
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.operation == ops.WORKER_DRAIN_SET
    assert event.outcome is Outcome.SUCCESS
    assert event.actor_id == user.id.value


def test_set_worker_drain_unknown_worker_records_nothing() -> None:
    recorder = RecordingAuditRecorder()
    app, _ = _base_app(recorder, platform_admin=True)
    app.dependency_overrides[get_set_worker_drain] = lambda: _FakeUseCase(result=None)
    client = next(_client(app))

    resp = client.put("/api/workers/ghost/drain")

    assert resp.status_code == 404
    assert recorder.events == []
