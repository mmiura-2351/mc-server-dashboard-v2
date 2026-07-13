"""Endpoint tests for the schedule router (issue #1837).

The HTTP boundary is exercised in-process via FastAPI's TestClient. Most tests
fake the use cases (NFR-TEST-1, no database) to verify the two-layer gate wiring
(non-member -> 404, read-without-permission -> 403), the domain-error -> HTTP
mappings, and that mutations are audited. A second group wires the *real* create
use case against an in-memory unit of work and a keyed permission checker to prove
the two-layer write gate end-to-end: ``schedule:manage`` without the action's own
permission is a 403 (anti-escalation).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    get_create_schedule,
    get_current_user,
    get_delete_schedule,
    get_list_schedule_runs,
    get_list_schedules,
    get_membership_visibility,
    get_permission_checker,
    get_preview_schedule,
    get_read_schedule,
    get_update_schedule,
)
from mc_server_dashboard_api.servers.adapters.cronsim_next_run_calculator import (
    CronsimNextRunCalculator,
)
from mc_server_dashboard_api.servers.application.schedules import (
    CreateSchedule,
    PreviewSchedule,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidCronExpressionError,
    InvalidSchedulePayloadError,
    InvalidScheduleTimezoneError,
    PermissionDeniedError,
    ScheduleNameAlreadyExistsError,
    ScheduleNotFoundError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.schedule import (
    Cadence,
    Schedule,
    ScheduleAction,
    ScheduleId,
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
from tests.identity.fakes import make_user
from tests.servers.fakes import FakeClock, FakeUnitOfWork

_NOW = dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.timezone.utc)


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


class _KeyedChecker(PermissionChecker):
    """Grants only the codes in ``allowed`` (ignoring resource), so a test can
    withhold a single permission and exercise the two-layer write gate."""

    def __init__(self, allowed: set[str]) -> None:
        self._allowed = allowed

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        return operation.value in self._allowed


class _RecordingRecorder:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def record(self, event: object) -> None:
        self.events.append(event)


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


def _schedule(server: uuid.UUID, *, enabled: bool = True) -> Schedule:
    return Schedule(
        id=ScheduleId.new(),
        server_id=ServerId(server),
        name="nightly",
        action=ScheduleAction.RESTART,
        cadence=Cadence.from_interval(3600),
        enabled=enabled,
        created_at=_NOW,
        updated_at=_NOW,
        next_run_at=_NOW if enabled else None,
    )


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
    read: _FakeUseCase | None = None,
    list_: _FakeUseCase | None = None,
    update: _FakeUseCase | None = None,
    delete: _FakeUseCase | None = None,
    runs: _FakeUseCase | None = None,
    preview: object | None = None,
    recorder: _RecordingRecorder | None = None,
) -> FastAPI:
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if create is not None:
        app.dependency_overrides[get_create_schedule] = lambda: create
    if read is not None:
        app.dependency_overrides[get_read_schedule] = lambda: read
    if list_ is not None:
        app.dependency_overrides[get_list_schedules] = lambda: list_
    if update is not None:
        app.dependency_overrides[get_update_schedule] = lambda: update
    if delete is not None:
        app.dependency_overrides[get_delete_schedule] = lambda: delete
    if runs is not None:
        app.dependency_overrides[get_list_schedule_runs] = lambda: runs
    if preview is not None:
        app.dependency_overrides[get_preview_schedule] = lambda: preview
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return app


def _url(
    community: uuid.UUID, server: uuid.UUID, *, schedule: uuid.UUID | None = None
) -> str:
    base = f"/api/communities/{community}/servers/{server}/schedules"
    return base if schedule is None else f"{base}/{schedule}"


def _create_body() -> dict[str, object]:
    return {"name": "nightly", "action": "restart", "interval_seconds": 3600}


# --- Layer-1 membership (non-member -> 404) ---------------------------------


def test_non_member_gets_404_on_create() -> None:
    app = _app(member=False, allow=True, create=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()), json=_create_body())
    assert resp.status_code == 404


def test_non_member_gets_404_on_list() -> None:
    app = _app(member=False, allow=True, list_=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 404


def test_non_member_gets_404_on_delete() -> None:
    app = _app(member=False, allow=True, delete=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), schedule=uuid.uuid4()))
    assert resp.status_code == 404


# --- read gate (member without schedule:read -> 403) ------------------------


def test_member_without_read_permission_gets_403_on_list() -> None:
    app = _app(member=True, allow=False, list_=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 403


def test_member_without_read_permission_gets_403_on_runs() -> None:
    app = _app(member=True, allow=False, runs=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), schedule=uuid.uuid4()) + "/runs")
    assert resp.status_code == 403


# --- error mappings ---------------------------------------------------------


def test_create_on_missing_server_is_404() -> None:
    app = _app(
        member=True, allow=True, create=_FakeUseCase(error=ServerNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()), json=_create_body())
    assert resp.status_code == 404


def test_create_duplicate_name_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=ScheduleNameAlreadyExistsError("nightly")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()), json=_create_body())
    assert resp.status_code == 409
    assert resp.json()["reason"] == "schedule_name_exists"


def test_create_invalid_cron_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=InvalidCronExpressionError("bad")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()), json=_create_body())
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_cron"


def test_create_invalid_payload_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=InvalidSchedulePayloadError("bad")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()), json=_create_body())
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_payload"


def test_create_invalid_timezone_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=InvalidScheduleTimezoneError("Mars/Olympus")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()), json=_create_body())
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_timezone"


def test_create_permission_denied_is_403_with_permission_member() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=PermissionDeniedError("server:command")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4()), json=_create_body())
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "forbidden"
    assert body["permission"] == "server:command"


def test_read_missing_schedule_is_404() -> None:
    app = _app(
        member=True, allow=True, read=_FakeUseCase(error=ScheduleNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), schedule=uuid.uuid4()))
    assert resp.status_code == 404


def test_update_missing_schedule_is_404() -> None:
    app = _app(
        member=True, allow=True, update=_FakeUseCase(error=ScheduleNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.patch(
        _url(uuid.uuid4(), uuid.uuid4(), schedule=uuid.uuid4()), json={"enabled": False}
    )
    assert resp.status_code == 404


def test_runs_missing_schedule_is_404() -> None:
    app = _app(
        member=True, allow=True, runs=_FakeUseCase(error=ScheduleNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), schedule=uuid.uuid4()) + "/runs")
    assert resp.status_code == 404


# --- happy paths + audit ----------------------------------------------------


def test_create_returns_201_and_audits() -> None:
    community = uuid.uuid4()
    server = uuid.uuid4()
    recorder = _RecordingRecorder()
    schedule = _schedule(server)
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(result=schedule),
        recorder=recorder,
    )
    client = next(_client(app))
    resp = client.post(_url(community, server), json=_create_body())
    assert resp.status_code == 201
    assert resp.json()["action"] == "restart"
    assert len(recorder.events) == 1


def test_read_disabled_schedule_reports_null_next_run() -> None:
    server = uuid.uuid4()
    schedule = _schedule(server, enabled=False)
    app = _app(member=True, allow=True, read=_FakeUseCase(result=schedule))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), server, schedule=uuid.uuid4()))
    assert resp.status_code == 200
    assert resp.json()["next_run_at"] is None


def test_delete_returns_204_and_audits() -> None:
    recorder = _RecordingRecorder()
    app = _app(member=True, allow=True, delete=_FakeUseCase(), recorder=recorder)
    client = next(_client(app))
    resp = client.delete(_url(uuid.uuid4(), uuid.uuid4(), schedule=uuid.uuid4()))
    assert resp.status_code == 204
    assert len(recorder.events) == 1


# --- full-stack two-layer write gate (real use case) ------------------------


def _server(community: uuid.UUID, server: uuid.UUID) -> Server:
    return Server(
        id=ServerId(server),
        community_id=ServersCommunityId(community),
        name=ServerName("srv"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _real_create_app(
    *, allowed: set[str], recorder: _RecordingRecorder | None = None
) -> tuple[FastAPI, uuid.UUID, uuid.UUID]:
    community = uuid.uuid4()
    server = uuid.uuid4()
    uow = FakeUnitOfWork()
    uow.servers.seed(_server(community, server))
    real = CreateSchedule(
        uow=uow, clock=FakeClock(_NOW), calculator=CronsimNextRunCalculator()
    )
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=True
    )
    app.dependency_overrides[get_permission_checker] = lambda: _KeyedChecker(allowed)
    app.dependency_overrides[get_create_schedule] = lambda: real
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return app, community, server


def test_manage_without_server_command_cannot_create_command_schedule() -> None:
    # The acceptance criterion: schedule:manage but not server:command -> 403.
    app, community, server = _real_create_app(allowed={"schedule:manage"})
    client = next(_client(app))
    resp = client.post(
        _url(community, server),
        json={
            "name": "broadcast",
            "action": "command",
            "command": "say hi",
            "interval_seconds": 3600,
        },
    )
    assert resp.status_code == 403
    assert resp.json()["permission"] == "server:command"


def test_full_authorization_creates_command_schedule() -> None:
    recorder = _RecordingRecorder()
    app, community, server = _real_create_app(
        allowed={"schedule:manage", "server:command"}, recorder=recorder
    )
    client = next(_client(app))
    resp = client.post(
        _url(community, server),
        json={
            "name": "broadcast",
            "action": "command",
            "command": "say hi",
            "interval_seconds": 3600,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["action"] == "command"
    assert body["command"] == "say hi"
    assert body["next_run_at"] is not None
    assert len(recorder.events) == 1


# --- preview endpoint (issue #1867) ------------------------------------------

_PREVIEW_URL_TEMPLATE = (
    "/api/communities/{community}/servers/{server}/schedules/preview"
)


def _preview_url(community: uuid.UUID, server: uuid.UUID) -> str:
    return _PREVIEW_URL_TEMPLATE.format(community=community, server=server)


def _preview_use_case() -> PreviewSchedule:
    return PreviewSchedule(clock=FakeClock(_NOW), calculator=CronsimNextRunCalculator())


def test_preview_cron_returns_5_datetimes() -> None:
    app = _app(member=True, allow=True, preview=_preview_use_case())
    client = next(_client(app))
    resp = client.post(
        _preview_url(uuid.uuid4(), uuid.uuid4()),
        json={"cron": "0 4 * * *", "timezone": "UTC"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["next_runs"]) == 5
    # All entries must be ISO datetime strings.
    for entry in body["next_runs"]:
        assert isinstance(entry, str)
        assert "T" in entry


def test_preview_interval_returns_5_datetimes() -> None:
    app = _app(member=True, allow=True, preview=_preview_use_case())
    client = next(_client(app))
    resp = client.post(
        _preview_url(uuid.uuid4(), uuid.uuid4()),
        json={"interval_seconds": 3600},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["next_runs"]) == 5


def test_preview_invalid_cron_is_422() -> None:
    app = _app(member=True, allow=True, preview=_preview_use_case())
    client = next(_client(app))
    resp = client.post(
        _preview_url(uuid.uuid4(), uuid.uuid4()),
        json={"cron": "not valid"},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_cron"


def test_preview_no_cadence_is_422() -> None:
    app = _app(member=True, allow=True, preview=_preview_use_case())
    client = next(_client(app))
    resp = client.post(
        _preview_url(uuid.uuid4(), uuid.uuid4()),
        json={},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_cadence"


def test_preview_non_member_gets_404() -> None:
    app = _app(member=False, allow=True, preview=_preview_use_case())
    client = next(_client(app))
    resp = client.post(
        _preview_url(uuid.uuid4(), uuid.uuid4()),
        json={"cron": "0 4 * * *"},
    )
    assert resp.status_code == 404


def test_preview_member_without_read_permission_gets_403() -> None:
    app = _app(member=True, allow=False, preview=_preview_use_case())
    client = next(_client(app))
    resp = client.post(
        _preview_url(uuid.uuid4(), uuid.uuid4()),
        json={"cron": "0 4 * * *"},
    )
    assert resp.status_code == 403
