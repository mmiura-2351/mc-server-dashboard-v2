"""Endpoint tests for the player-group router (issue #276).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies the
two-layer gate per route (non-member -> 404, member-without-permission -> 403,
authorized -> 2xx), the domain-error -> HTTP mappings (unknown kind 422, duplicate
name 409, cross-community/missing 404), and that a mutation is audited.
"""

from __future__ import annotations

import uuid

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
    get_add_player,
    get_attach_group,
    get_audit_recorder,
    get_create_group,
    get_current_user,
    get_delete_group,
    get_membership_visibility,
    get_permission_checker,
    get_read_group,
    get_remove_player,
    get_rename_group,
)
from mc_server_dashboard_api.servers.domain.errors import (
    GroupNameAlreadyExistsError,
    GroupNotFoundError,
    GroupPlayerEditConflictError,
    InvalidGroupKindError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from tests.client_utils import enter_client
from tests.identity.fakes import make_user


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


class _RecordingRecorder:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def record(self, event: object) -> None:
        self.events.append(event)


def _client(app: object) -> TestClient:
    return enter_client(TestClient(app))  # type: ignore[arg-type]


def _group(community: uuid.UUID, *, kind: GroupKind = GroupKind.OP) -> PlayerGroup:
    return PlayerGroup(
        id=GroupId.new(),
        community_id=ServersCommunityId(community),
        name=GroupName("admins"),
        kind=kind,
        players=[],
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
    rename: _FakeUseCase | None = None,
    delete: _FakeUseCase | None = None,
    add_player: _FakeUseCase | None = None,
    remove_player: _FakeUseCase | None = None,
    attach: _FakeUseCase | None = None,
    recorder: _RecordingRecorder | None = None,
) -> object:
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if create is not None:
        app.dependency_overrides[get_create_group] = lambda: create
    if read is not None:
        app.dependency_overrides[get_read_group] = lambda: read
    if rename is not None:
        app.dependency_overrides[get_rename_group] = lambda: rename
    if delete is not None:
        app.dependency_overrides[get_delete_group] = lambda: delete
    if add_player is not None:
        app.dependency_overrides[get_add_player] = lambda: add_player
    if remove_player is not None:
        app.dependency_overrides[get_remove_player] = lambda: remove_player
    if attach is not None:
        app.dependency_overrides[get_attach_group] = lambda: attach
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return app


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_create() -> None:
    app = _app(member=False, allow=True, create=_FakeUseCase())
    client = _client(app)
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/groups", json={"name": "ops", "kind": "op"}
    )
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_create() -> None:
    app = _app(member=True, allow=False, create=_FakeUseCase())
    client = _client(app)
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/groups", json={"name": "ops", "kind": "op"}
    )
    assert resp.status_code == 403


def test_authorized_member_creates_group_and_audits() -> None:
    community = uuid.uuid4()
    group = _group(community)
    recorder = _RecordingRecorder()
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(result=group),
        recorder=recorder,
    )
    client = _client(app)
    resp = client.post(
        f"/api/communities/{community}/groups", json={"name": "admins", "kind": "op"}
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "op"
    assert len(recorder.events) == 1


# --- error mappings --------------------------------------------------------


def test_create_unknown_kind_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=InvalidGroupKindError("banned")),
    )
    client = _client(app)
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/groups", json={"name": "x", "kind": "banned"}
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_group_kind"


def test_create_duplicate_name_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        create=_FakeUseCase(error=GroupNameAlreadyExistsError("admins")),
    )
    client = _client(app)
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/groups", json={"name": "admins", "kind": "op"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "group_name_exists"


def _edit_conflict() -> GroupPlayerEditConflictError:
    # ``save`` replaces the player set wholesale, so an interleaved edit on the
    # same group can collide on uq_group_player_group_uuid from any of the three
    # routes that reach it -- the rename re-inserts the set too (issue #2613).
    return GroupPlayerEditConflictError("uq_group_player_group_uuid")


def test_rename_losing_an_interleaved_player_edit_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        rename=_FakeUseCase(error=_edit_conflict()),
        recorder=_RecordingRecorder(),
    )
    client = _client(app)
    resp = client.patch(
        f"/api/communities/{uuid.uuid4()}/groups/{uuid.uuid4()}",
        json={"name": "admins"},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "group_edit_conflict"


def test_add_player_losing_an_interleaved_player_edit_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        add_player=_FakeUseCase(error=_edit_conflict()),
        recorder=_RecordingRecorder(),
    )
    client = _client(app)
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/groups/{uuid.uuid4()}/players",
        json={"uuid": str(uuid.uuid4()), "username": "alice"},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "group_edit_conflict"


def test_remove_player_losing_an_interleaved_player_edit_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        remove_player=_FakeUseCase(error=_edit_conflict()),
        recorder=_RecordingRecorder(),
    )
    client = _client(app)
    resp = client.delete(
        f"/api/communities/{uuid.uuid4()}/groups/{uuid.uuid4()}/players/{uuid.uuid4()}"
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "group_edit_conflict"


def test_read_missing_group_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        read=_FakeUseCase(error=GroupNotFoundError("x")),
    )
    client = _client(app)
    resp = client.get(f"/api/communities/{uuid.uuid4()}/groups/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_attach_missing_server_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        attach=_FakeUseCase(error=ServerNotFoundError("x")),
        recorder=_RecordingRecorder(),
    )
    client = _client(app)
    resp = client.put(
        f"/api/communities/{uuid.uuid4()}/groups/{uuid.uuid4()}/servers/{uuid.uuid4()}"
    )
    assert resp.status_code == 404


def test_delete_group_returns_204() -> None:
    app = _app(
        member=True,
        allow=True,
        delete=_FakeUseCase(),
        recorder=_RecordingRecorder(),
    )
    client = _client(app)
    resp = client.delete(f"/api/communities/{uuid.uuid4()}/groups/{uuid.uuid4()}")
    assert resp.status_code == 204
