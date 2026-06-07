"""Endpoint tests for the account self-service routes on /users/me.

The use cases are overridden with fakes so no database is touched (NFR-TEST-1).
Verifies status codes, the uniform 401 on a wrong current password, the policy /
uniqueness reason mappings, the owner / last-admin 409 refusals, and that each
operation records its audit event.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_change_password,
    get_current_user,
    get_delete_account,
    get_update_profile,
)
from mc_server_dashboard_api.identity.domain.errors import (
    CommunityOwnedError,
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    LastPlatformAdminError,
    PasswordPolicyError,
    UsernameAlreadyExistsError,
    UserNotFoundError,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.identity.fakes import make_user

_VALID_PASSWORD = "Np4@xZ#Lq9wR"


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
    "change_password": get_change_password,
    "update_profile": get_update_profile,
    "delete_account": get_delete_account,
    "recorder": get_audit_recorder,
}


def _client(user: object, **overrides: object) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_current_user] = _provider(user)
    for dependency, value in overrides.items():
        app.dependency_overrides[_PROVIDERS[dependency]] = _provider(value)
    with TestClient(app) as client:
        yield client


# --- PUT /users/me/password ------------------------------------------------


def test_change_password_returns_204_and_audits() -> None:
    user = make_user()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(user, change_password=fake, recorder=recorder))
    resp = client.put(
        "/users/me/password",
        json={"current_password": "old", "new_password": _VALID_PASSWORD},
    )
    assert resp.status_code == 204
    assert fake.calls == [
        {
            "user_id": user.id,
            "current_password": "old",
            "new_password": _VALID_PASSWORD,
        }
    ]
    assert [e.operation for e in recorder.events] == [ops.AUTH_PASSWORD_CHANGE]
    assert recorder.events[0].actor_id == user.id.value
    assert recorder.events[0].outcome == Outcome.SUCCESS


def test_change_password_wrong_current_returns_uniform_401() -> None:
    user = make_user()
    fake = _Fake(error=InvalidCredentialsError())
    client = next(_client(user, change_password=fake))
    resp = client.put(
        "/users/me/password",
        json={"current_password": "bad", "new_password": _VALID_PASSWORD},
    )
    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_credentials"


def test_change_password_weak_new_returns_422_with_reason_no_echo() -> None:
    user = make_user()
    fake = _Fake(error=PasswordPolicyError("too_short"))
    client = next(_client(user, change_password=fake))
    weak = "Qz9!pw"
    resp = client.put(
        "/users/me/password",
        json={"current_password": "old", "new_password": weak},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "too_short"
    assert weak not in resp.text


def test_change_password_user_gone_returns_401_invalid_token() -> None:
    # A concurrent self-delete races between get_current_user and the use case's
    # get_by_id: the use case raises UserNotFoundError and the route returns the
    # same 401 invalid_token as an invalidated token, not a 500.
    user = make_user()
    fake = _Fake(error=UserNotFoundError(str(user.id.value)))
    client = next(_client(user, change_password=fake))
    resp = client.put(
        "/users/me/password",
        json={"current_password": "old", "new_password": _VALID_PASSWORD},
    )
    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_token"


def test_change_password_requires_auth() -> None:
    fake = _Fake(result=None)
    app = create_app()
    app.dependency_overrides[get_change_password] = _provider(fake)
    with TestClient(app) as client:
        resp = client.put(
            "/users/me/password",
            json={"current_password": "old", "new_password": _VALID_PASSWORD},
        )
    assert resp.status_code == 401


# --- PATCH /users/me -------------------------------------------------------


def test_update_profile_returns_user_and_audits() -> None:
    user = make_user(username="alice", email="alice@example.com")
    updated = make_user(username="alice2", email="alice2@example.com")
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=updated)
    client = next(_client(user, update_profile=fake, recorder=recorder))
    resp = client.patch(
        "/users/me", json={"username": "alice2", "email": "alice2@example.com"}
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == "alice2"
    assert fake.calls == [
        {"user_id": user.id, "username": "alice2", "email": "alice2@example.com"}
    ]
    assert [e.operation for e in recorder.events] == [ops.AUTH_PROFILE_UPDATE]


def test_update_profile_username_conflict_returns_409() -> None:
    user = make_user()
    fake = _Fake(error=UsernameAlreadyExistsError("taken"))
    client = next(_client(user, update_profile=fake))
    resp = client.patch("/users/me", json={"username": "taken"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "username_taken"


def test_update_profile_email_conflict_returns_409() -> None:
    user = make_user()
    fake = _Fake(error=EmailAlreadyExistsError("taken@example.com"))
    client = next(_client(user, update_profile=fake))
    resp = client.patch("/users/me", json={"email": "taken@example.com"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "email_taken"


def test_update_profile_user_gone_returns_401_invalid_token() -> None:
    user = make_user()
    fake = _Fake(error=UserNotFoundError(str(user.id.value)))
    client = next(_client(user, update_profile=fake))
    resp = client.patch("/users/me", json={"username": "alice2"})
    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_token"


# --- DELETE /users/me ------------------------------------------------------


def test_delete_account_returns_204_and_audits() -> None:
    user = make_user()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(user, delete_account=fake, recorder=recorder))
    resp = client.request("DELETE", "/users/me", json={"password": _VALID_PASSWORD})
    assert resp.status_code == 204
    assert fake.calls == [{"user_id": user.id, "password": _VALID_PASSWORD}]
    assert [e.operation for e in recorder.events] == [ops.AUTH_ACCOUNT_DELETE]
    assert recorder.events[0].actor_id == user.id.value
    assert recorder.events[0].target_id == user.id.value


def test_delete_account_wrong_password_returns_uniform_401() -> None:
    # Re-auth (issue #420): a wrong password is the same uniform 401 the
    # change-password endpoint returns, so it is no confirmation oracle.
    user = make_user()
    fake = _Fake(error=InvalidCredentialsError())
    client = next(_client(user, delete_account=fake))
    resp = client.request("DELETE", "/users/me", json={"password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_credentials"


def test_delete_account_missing_password_returns_422() -> None:
    user = make_user()
    fake = _Fake(result=None)
    client = next(_client(user, delete_account=fake))
    resp = client.request("DELETE", "/users/me", json={})
    assert resp.status_code == 422
    assert fake.calls == []


def test_delete_account_blank_password_returns_422() -> None:
    user = make_user()
    fake = _Fake(result=None)
    client = next(_client(user, delete_account=fake))
    resp = client.request("DELETE", "/users/me", json={"password": ""})
    assert resp.status_code == 422
    assert fake.calls == []


def test_delete_account_owner_returns_409() -> None:
    user = make_user()
    fake = _Fake(error=CommunityOwnedError(str(uuid.uuid4())))
    client = next(_client(user, delete_account=fake))
    resp = client.request("DELETE", "/users/me", json={"password": _VALID_PASSWORD})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "owns_community"


def test_delete_account_last_admin_returns_409() -> None:
    user = make_user()
    fake = _Fake(error=LastPlatformAdminError(str(uuid.uuid4())))
    client = next(_client(user, delete_account=fake))
    resp = client.request("DELETE", "/users/me", json={"password": _VALID_PASSWORD})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "last_platform_admin"


def test_delete_account_user_gone_returns_401_invalid_token() -> None:
    user = make_user()
    fake = _Fake(error=UserNotFoundError(str(user.id.value)))
    client = next(_client(user, delete_account=fake))
    resp = client.request("DELETE", "/users/me", json={"password": _VALID_PASSWORD})
    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_token"
