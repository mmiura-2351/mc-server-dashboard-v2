"""Endpoint tests for the platform-admin user-administration routes (#278).

The use cases are overridden with fakes so no database is touched (NFR-TEST-1).
Verifies the platform-admin gate, the listing shape, the deactivate/reactivate/
delete/grant-revoke status codes, the 409 refusal reasons (self_target,
last_platform_admin, owns_community), the 404 on an unknown target, and that each
mutating route records its audit event.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.dependencies import (
    get_admin_delete_user,
    get_audit_recorder,
    get_current_user,
    get_list_users,
    get_set_platform_admin,
    get_set_user_active,
)
from mc_server_dashboard_api.identity.application.list_users import UserPage
from mc_server_dashboard_api.identity.domain.errors import (
    CommunityOwnedError,
    LastPlatformAdminError,
    SelfTargetError,
    UserNotFoundError,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.identity.fakes import make_user


class _Fake:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


def _provider(value: object) -> Callable[[], object]:
    def _provide() -> object:
        return value

    return _provide


_PROVIDERS = {
    "list_users": get_list_users,
    "set_user_active": get_set_user_active,
    "admin_delete_user": get_admin_delete_user,
    "set_platform_admin": get_set_platform_admin,
    "recorder": get_audit_recorder,
}


def _client(
    *, platform_admin: bool = True, **overrides: object
) -> Iterator[TestClient]:
    app = create_app()
    admin = make_user(username="admin", is_platform_admin=platform_admin)
    app.dependency_overrides[get_current_user] = _provider(admin)
    for dependency, value in overrides.items():
        app.dependency_overrides[_PROVIDERS[dependency]] = _provider(value)
    with TestClient(app) as client:
        yield client


# --- GET /users ------------------------------------------------------------


def test_list_users_requires_platform_admin() -> None:
    client = next(_client(platform_admin=False, list_users=_Fake()))
    assert client.get("/users").status_code == 403


def test_list_users_returns_page() -> None:
    users = [make_user(username="a", email="a@example.com")]
    fake = _Fake(result=UserPage(users=users, total=1))
    client = next(_client(list_users=fake))
    resp = client.get("/users?limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert body["users"][0]["username"] == "a"
    assert body["users"][0]["active"] is True
    assert "created_at" in body["users"][0]
    assert fake.calls == [{"limit": 10, "offset": 0}]


def test_list_users_default_pagination() -> None:
    fake = _Fake(result=UserPage(users=[], total=0))
    client = next(_client(list_users=fake))
    resp = client.get("/users")
    assert resp.status_code == 200
    assert fake.calls == [{"limit": 50, "offset": 0}]


def test_list_users_rejects_out_of_range_limit() -> None:
    fake = _Fake(result=UserPage(users=[], total=0))
    client = next(_client(list_users=fake))
    assert client.get("/users?limit=0").status_code == 422
    assert client.get("/users?limit=101").status_code == 422


# --- POST /users/{id}/deactivate -------------------------------------------


def test_deactivate_returns_204_and_audits() -> None:
    target = uuid.uuid4()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(set_user_active=fake, recorder=recorder))
    resp = client.post(f"/users/{target}/deactivate")
    assert resp.status_code == 204
    assert fake.calls[0]["active"] is False
    assert [e.operation for e in recorder.events] == [ops.USER_DEACTIVATE]
    assert recorder.events[0].target_id == target


def test_deactivate_requires_platform_admin() -> None:
    client = next(_client(platform_admin=False, set_user_active=_Fake()))
    assert client.post(f"/users/{uuid.uuid4()}/deactivate").status_code == 403


def test_deactivate_self_returns_409_self_target() -> None:
    fake = _Fake(error=SelfTargetError(str(uuid.uuid4())))
    client = next(_client(set_user_active=fake))
    resp = client.post(f"/users/{uuid.uuid4()}/deactivate")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "self_target"


def test_deactivate_last_admin_returns_409() -> None:
    fake = _Fake(error=LastPlatformAdminError(str(uuid.uuid4())))
    client = next(_client(set_user_active=fake))
    resp = client.post(f"/users/{uuid.uuid4()}/deactivate")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "last_platform_admin"


def test_deactivate_unknown_returns_404() -> None:
    fake = _Fake(error=UserNotFoundError(str(uuid.uuid4())))
    client = next(_client(set_user_active=fake))
    assert client.post(f"/users/{uuid.uuid4()}/deactivate").status_code == 404


# --- POST /users/{id}/reactivate -------------------------------------------


def test_reactivate_returns_204_and_audits() -> None:
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(set_user_active=fake, recorder=recorder))
    resp = client.post(f"/users/{uuid.uuid4()}/reactivate")
    assert resp.status_code == 204
    assert fake.calls[0]["active"] is True
    assert [e.operation for e in recorder.events] == [ops.USER_REACTIVATE]


def test_reactivate_unknown_returns_404() -> None:
    fake = _Fake(error=UserNotFoundError(str(uuid.uuid4())))
    client = next(_client(set_user_active=fake))
    assert client.post(f"/users/{uuid.uuid4()}/reactivate").status_code == 404


# --- DELETE /users/{id} ----------------------------------------------------


def test_delete_returns_204_and_audits() -> None:
    target = uuid.uuid4()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(admin_delete_user=fake, recorder=recorder))
    resp = client.delete(f"/users/{target}")
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.USER_DELETE]
    assert recorder.events[0].target_id == target


def test_delete_self_returns_409_self_target() -> None:
    fake = _Fake(error=SelfTargetError(str(uuid.uuid4())))
    client = next(_client(admin_delete_user=fake))
    resp = client.delete(f"/users/{uuid.uuid4()}")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "self_target"


def test_delete_owner_returns_409() -> None:
    fake = _Fake(error=CommunityOwnedError(str(uuid.uuid4())))
    client = next(_client(admin_delete_user=fake))
    resp = client.delete(f"/users/{uuid.uuid4()}")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "owns_community"


def test_delete_last_admin_returns_409() -> None:
    fake = _Fake(error=LastPlatformAdminError(str(uuid.uuid4())))
    client = next(_client(admin_delete_user=fake))
    resp = client.delete(f"/users/{uuid.uuid4()}")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "last_platform_admin"


def test_delete_requires_platform_admin() -> None:
    client = next(_client(platform_admin=False, admin_delete_user=_Fake()))
    assert client.delete(f"/users/{uuid.uuid4()}").status_code == 403


# --- PUT /users/{id}/platform-admin ----------------------------------------


def test_grant_platform_admin_returns_204_and_audits() -> None:
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(set_platform_admin=fake, recorder=recorder))
    resp = client.put(f"/users/{uuid.uuid4()}/platform-admin", json={"grant": True})
    assert resp.status_code == 204
    assert fake.calls[0]["grant"] is True
    assert [e.operation for e in recorder.events] == [ops.USER_PLATFORM_ADMIN_GRANT]


def test_revoke_platform_admin_returns_204_and_audits() -> None:
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(set_platform_admin=fake, recorder=recorder))
    resp = client.put(f"/users/{uuid.uuid4()}/platform-admin", json={"grant": False})
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.USER_PLATFORM_ADMIN_REVOKE]


def test_revoke_last_admin_returns_409() -> None:
    fake = _Fake(error=LastPlatformAdminError(str(uuid.uuid4())))
    client = next(_client(set_platform_admin=fake))
    resp = client.put(f"/users/{uuid.uuid4()}/platform-admin", json={"grant": False})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "last_platform_admin"


def test_set_platform_admin_requires_platform_admin() -> None:
    client = next(_client(platform_admin=False, set_platform_admin=_Fake()))
    resp = client.put(f"/users/{uuid.uuid4()}/platform-admin", json={"grant": True})
    assert resp.status_code == 403
