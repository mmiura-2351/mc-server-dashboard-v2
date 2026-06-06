"""Platform-admin operational endpoints for the versions context (issue #286).

In-process via TestClient with the use cases overridden by fakes (no network).
Covers: the manual catalog refresh (all / one type / unknown-type 404), the
JAR-pool stats read, the platform-admin gate (non-admin -> 403), and the refresh
audit record.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_catalog_refresh,
    get_current_user,
    get_jar_pool_gc,
    get_jar_pool_stats,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)
from mc_server_dashboard_api.storage.domain.port import JarPoolStats
from mc_server_dashboard_api.versions.application.jar_gc import JarGcResult
from mc_server_dashboard_api.versions.domain.errors import UnknownServerTypeError
from mc_server_dashboard_api.versions.domain.value_objects import ServerType


class _FakeRefresh:
    def __init__(self) -> None:
        self.calls: list[ServerType | None] = []

    async def __call__(self, *, server_type: ServerType | None) -> list[ServerType]:
        self.calls.append(server_type)
        if server_type is None:
            return list(ServerType)
        return [server_type]


class _UnknownTypeRefresh:
    async def __call__(self, *, server_type: ServerType | None) -> list[ServerType]:
        raise UnknownServerTypeError("forge")


class _FakeStats:
    async def __call__(self) -> JarPoolStats:
        return JarPoolStats(count=3, total_bytes=4096)


class _FakeGc:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> JarGcResult:
        self.calls += 1
        return JarGcResult(scanned=5, deleted=2, freed_bytes=2048)


class _RecordingRecorder:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def record(self, event: object) -> None:
        self.events.append(event)


def _user(*, admin: bool) -> User:
    return User(
        id=UserId(uuid.uuid4()),
        username=Username("tester"),
        email=EmailAddress("tester@example.test"),
        password_hash="x",
        is_platform_admin=admin,
        created_at=dt.datetime.now(dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
    )


def _client(
    *,
    admin: bool = True,
    refresh: object | None = None,
    stats: object | None = None,
    gc: object | None = None,
    recorder: object | None = None,
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _user(admin=admin)
    if refresh is not None:
        app.dependency_overrides[get_catalog_refresh] = lambda: refresh
    if stats is not None:
        app.dependency_overrides[get_jar_pool_stats] = lambda: stats
    if gc is not None:
        app.dependency_overrides[get_jar_pool_gc] = lambda: gc
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return TestClient(app)


def test_refresh_all_returns_invalidated_catalogs() -> None:
    refresh = _FakeRefresh()
    client = _client(refresh=refresh, recorder=_RecordingRecorder())
    with client:
        resp = client.post("/versions/refresh")
    assert resp.status_code == 200
    assert set(resp.json()["invalidated"]) == {"vanilla", "paper", "fabric", "forge"}
    assert refresh.calls == [None]


def test_refresh_one_type_filters() -> None:
    refresh = _FakeRefresh()
    client = _client(refresh=refresh, recorder=_RecordingRecorder())
    with client:
        resp = client.post("/versions/refresh?server_type=paper")
    assert resp.status_code == 200
    assert resp.json()["invalidated"] == ["paper"]
    assert refresh.calls == [ServerType.PAPER]


def test_refresh_unknown_type_is_404() -> None:
    client = _client(refresh=_UnknownTypeRefresh(), recorder=_RecordingRecorder())
    with client:
        resp = client.post("/versions/refresh?server_type=forge")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "unknown_server_type"


def test_refresh_requires_platform_admin() -> None:
    client = _client(admin=False, refresh=_FakeRefresh())
    with client:
        resp = client.post("/versions/refresh")
    assert resp.status_code == 403


def test_refresh_is_audited() -> None:
    recorder = _RecordingRecorder()
    client = _client(refresh=_FakeRefresh(), recorder=recorder)
    with client:
        resp = client.post("/versions/refresh")
    assert resp.status_code == 200
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.operation == ops.VERSION_REFRESH  # type: ignore[attr-defined]


def test_jar_pool_stats_returns_count_and_bytes() -> None:
    client = _client(stats=_FakeStats())
    with client:
        resp = client.get("/versions/jar-pool/stats")
    assert resp.status_code == 200
    assert resp.json() == {"count": 3, "total_bytes": 4096}


def test_jar_pool_stats_requires_platform_admin() -> None:
    client = _client(admin=False, stats=_FakeStats())
    with client:
        resp = client.get("/versions/jar-pool/stats")
    assert resp.status_code == 403


def test_jar_pool_gc_returns_scanned_deleted_freed() -> None:
    gc = _FakeGc()
    client = _client(gc=gc, recorder=_RecordingRecorder())
    with client:
        resp = client.post("/versions/jar-pool/gc")
    assert resp.status_code == 200
    assert resp.json() == {"scanned": 5, "deleted": 2, "freed_bytes": 2048}
    assert gc.calls == 1


def test_jar_pool_gc_requires_platform_admin() -> None:
    client = _client(admin=False, gc=_FakeGc())
    with client:
        resp = client.post("/versions/jar-pool/gc")
    assert resp.status_code == 403


def test_jar_pool_gc_is_audited() -> None:
    recorder = _RecordingRecorder()
    client = _client(gc=_FakeGc(), recorder=recorder)
    with client:
        resp = client.post("/versions/jar-pool/gc")
    assert resp.status_code == 200
    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.operation == ops.VERSION_JAR_GC  # type: ignore[attr-defined]
