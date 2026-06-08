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
    get_admin_create_user,
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
    PasswordPolicyError,
    SelfTargetError,
    UsernameAlreadyExistsError,
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
    "admin_create_user": get_admin_create_user,
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


# --- GET /admin/users ------------------------------------------------------


def test_list_users_requires_platform_admin() -> None:
    client = next(_client(platform_admin=False, list_users=_Fake()))
    assert client.get("/api/admin/users").status_code == 403


def test_list_users_returns_page() -> None:
    users = [make_user(username="a", email="a@example.com")]
    fake = _Fake(result=UserPage(users=users, total=1))
    client = next(_client(list_users=fake))
    resp = client.get("/api/admin/users?limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert body["users"][0]["username"] == "a"
    assert body["users"][0]["active"] is True
    # Canonical RFC 3339 UTC form: the ``Z`` suffix, not ``+00:00`` (issue #632).
    assert body["users"][0]["created_at"] == "2026-06-04T00:00:00Z"
    assert fake.calls == [{"limit": 10, "offset": 0}]


def test_list_users_default_pagination() -> None:
    fake = _Fake(result=UserPage(users=[], total=0))
    client = next(_client(list_users=fake))
    resp = client.get("/api/admin/users")
    assert resp.status_code == 200
    assert fake.calls == [{"limit": 50, "offset": 0}]


def test_list_users_rejects_out_of_range_limit() -> None:
    fake = _Fake(result=UserPage(users=[], total=0))
    client = next(_client(list_users=fake))
    assert client.get("/api/admin/users?limit=0").status_code == 422
    assert client.get("/api/admin/users?limit=101").status_code == 422


# --- POST /admin/users/{id}/deactivate -------------------------------------


def test_deactivate_returns_204_and_audits() -> None:
    target = uuid.uuid4()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(set_user_active=fake, recorder=recorder))
    resp = client.post(f"/api/admin/users/{target}/deactivate")
    assert resp.status_code == 204
    assert fake.calls[0]["active"] is False
    assert [e.operation for e in recorder.events] == [ops.USER_DEACTIVATE]
    assert recorder.events[0].target_id == target


def test_deactivate_requires_platform_admin() -> None:
    client = next(_client(platform_admin=False, set_user_active=_Fake()))
    assert client.post(f"/api/admin/users/{uuid.uuid4()}/deactivate").status_code == 403


def test_deactivate_self_returns_409_self_target() -> None:
    fake = _Fake(error=SelfTargetError(str(uuid.uuid4())))
    client = next(_client(set_user_active=fake))
    resp = client.post(f"/api/admin/users/{uuid.uuid4()}/deactivate")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "self_target"


def test_deactivate_last_admin_returns_409() -> None:
    fake = _Fake(error=LastPlatformAdminError(str(uuid.uuid4())))
    client = next(_client(set_user_active=fake))
    resp = client.post(f"/api/admin/users/{uuid.uuid4()}/deactivate")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "last_platform_admin"


def test_deactivate_unknown_returns_404() -> None:
    fake = _Fake(error=UserNotFoundError(str(uuid.uuid4())))
    client = next(_client(set_user_active=fake))
    assert client.post(f"/api/admin/users/{uuid.uuid4()}/deactivate").status_code == 404


# --- POST /admin/users/{id}/reactivate -------------------------------------


def test_reactivate_returns_204_and_audits() -> None:
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(set_user_active=fake, recorder=recorder))
    resp = client.post(f"/api/admin/users/{uuid.uuid4()}/reactivate")
    assert resp.status_code == 204
    assert fake.calls[0]["active"] is True
    assert [e.operation for e in recorder.events] == [ops.USER_REACTIVATE]


def test_reactivate_unknown_returns_404() -> None:
    fake = _Fake(error=UserNotFoundError(str(uuid.uuid4())))
    client = next(_client(set_user_active=fake))
    assert client.post(f"/api/admin/users/{uuid.uuid4()}/reactivate").status_code == 404


# --- DELETE /admin/users/{id} ----------------------------------------------


def test_delete_returns_204_and_audits() -> None:
    target = uuid.uuid4()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(admin_delete_user=fake, recorder=recorder))
    resp = client.delete(f"/api/admin/users/{target}")
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.USER_DELETE]
    assert recorder.events[0].target_id == target


def test_delete_self_returns_409_self_target() -> None:
    fake = _Fake(error=SelfTargetError(str(uuid.uuid4())))
    client = next(_client(admin_delete_user=fake))
    resp = client.delete(f"/api/admin/users/{uuid.uuid4()}")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "self_target"


def test_delete_owner_returns_409() -> None:
    fake = _Fake(error=CommunityOwnedError(str(uuid.uuid4())))
    client = next(_client(admin_delete_user=fake))
    resp = client.delete(f"/api/admin/users/{uuid.uuid4()}")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "owns_community"


def test_delete_last_admin_returns_409() -> None:
    fake = _Fake(error=LastPlatformAdminError(str(uuid.uuid4())))
    client = next(_client(admin_delete_user=fake))
    resp = client.delete(f"/api/admin/users/{uuid.uuid4()}")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "last_platform_admin"


def test_delete_requires_platform_admin() -> None:
    client = next(_client(platform_admin=False, admin_delete_user=_Fake()))
    assert client.delete(f"/api/admin/users/{uuid.uuid4()}").status_code == 403


# --- PUT /admin/users/{id}/platform-admin ----------------------------------


def test_grant_platform_admin_returns_204_and_audits() -> None:
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(set_platform_admin=fake, recorder=recorder))
    resp = client.put(
        f"/api/admin/users/{uuid.uuid4()}/platform-admin", json={"grant": True}
    )
    assert resp.status_code == 204
    assert fake.calls[0]["grant"] is True
    assert [e.operation for e in recorder.events] == [ops.USER_PLATFORM_ADMIN_GRANT]


def test_revoke_platform_admin_returns_204_and_audits() -> None:
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(set_platform_admin=fake, recorder=recorder))
    resp = client.put(
        f"/api/admin/users/{uuid.uuid4()}/platform-admin", json={"grant": False}
    )
    assert resp.status_code == 204
    assert [e.operation for e in recorder.events] == [ops.USER_PLATFORM_ADMIN_REVOKE]


def test_revoke_last_admin_returns_409() -> None:
    fake = _Fake(error=LastPlatformAdminError(str(uuid.uuid4())))
    client = next(_client(set_platform_admin=fake))
    resp = client.put(
        f"/api/admin/users/{uuid.uuid4()}/platform-admin", json={"grant": False}
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "last_platform_admin"


def test_set_platform_admin_requires_platform_admin() -> None:
    client = next(_client(platform_admin=False, set_platform_admin=_Fake()))
    resp = client.put(
        f"/api/admin/users/{uuid.uuid4()}/platform-admin", json={"grant": True}
    )
    assert resp.status_code == 403


# --- POST /admin/users (admin creation, #368) ------------------------------

_VALID_PASSWORD = "Wm7!qz#Lp2vT"


def _create_payload() -> dict[str, str]:
    return {
        "username": "bob",
        "email": "bob@example.com",
        "password": _VALID_PASSWORD,
    }


def test_admin_create_returns_201_and_user_without_hash_and_audits() -> None:
    created = make_user(username="bob", email="bob@example.com")
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=created)
    client = next(_client(admin_create_user=fake, recorder=recorder))
    resp = client.post("/api/admin/users", json=_create_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body == {
        "id": str(created.id.value),
        "username": "bob",
        "email": "bob@example.com",
        "is_platform_admin": False,
    }
    assert "password" not in body
    assert created.password_hash not in resp.text
    assert fake.calls == [
        {"username": "bob", "email": "bob@example.com", "password": _VALID_PASSWORD}
    ]
    # The admin is the actor; the new user is the target (FR-AUD-1).
    assert [e.operation for e in recorder.events] == [ops.USER_CREATE]
    assert recorder.events[0].target_id == created.id.value


def test_admin_create_requires_platform_admin() -> None:
    fake = _Fake(result=make_user())
    client = next(_client(platform_admin=False, admin_create_user=fake))
    resp = client.post("/api/admin/users", json=_create_payload())
    assert resp.status_code == 403
    # The use case must not run for a non-admin.
    assert fake.calls == []


def test_admin_create_weak_password_returns_422_no_echo() -> None:
    fake = _Fake(error=PasswordPolicyError("too_short"))
    client = next(_client(admin_create_user=fake))
    weak = "Qz9!secretpw"
    resp = client.post(
        "/api/admin/users",
        json={"username": "bob", "email": "bob@example.com", "password": weak},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "too_short"
    assert weak not in resp.text


def test_admin_create_overlong_password_422_no_echo() -> None:
    # Structural validation (FastAPI ``string_too_long``, max_length=1024) fails
    # before the use case runs; the central handler must scrub the submitted
    # value so the plaintext password never reaches the response body (#393).
    # The domain-policy no-echo path is covered above; this pins the structural
    # FastAPI-level 422 that previously leaked via ``errors[].input``.
    fake = _Fake(result=make_user())
    client = next(_client(admin_create_user=fake))
    overlong = "Aa1!" + "x" * 1025
    resp = client.post(
        "/api/admin/users",
        json={"username": "bob", "email": "bob@example.com", "password": overlong},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "validation_error"
    assert overlong not in resp.text
    # The structural failure short-circuits before the use case.
    assert fake.calls == []


def test_admin_create_duplicate_username_returns_409() -> None:
    fake = _Fake(error=UsernameAlreadyExistsError("bob"))
    client = next(_client(admin_create_user=fake))
    resp = client.post("/api/admin/users", json=_create_payload())
    assert resp.status_code == 409
    assert resp.json()["reason"] == "username_taken"
