"""Endpoint tests for POST /users with the RegisterUser use case faked.

The HTTP boundary is exercised in-process via FastAPI's TestClient; the use case
is overridden so no database is touched (NFR-TEST-1). Verifies the success code,
the response shape (never the hash), and the policy/duplicate error mappings.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.dependencies import get_audit_recorder, get_register_user
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    PasswordPolicyError,
    RegistrationDisabledError,
    RegistrationThrottledError,
    UsernameAlreadyExistsError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)
from tests.audit.fakes import RecordingAuditRecorder

_VALID_PASSWORD = "Wm7!qz#Lp2vT"


class _FakeRegisterUser:
    def __init__(self, *, result: User | None = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.calls: list[dict[str, str]] = []

    async def __call__(
        self, *, username: str, email: str, password: str, ip: str | None = None
    ) -> User:
        self.calls.append({"username": username, "email": email, "password": password})
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _client(
    use_case: _FakeRegisterUser, recorder: RecordingAuditRecorder | None = None
) -> Iterator[TestClient]:
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_register_user] = lambda: use_case
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    with TestClient(app) as client:
        yield client


def _user(*, is_platform_admin: bool = False) -> User:
    now = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
    return User(
        id=UserId.new(),
        username=Username("alice"),
        email=EmailAddress("alice@example.com"),
        password_hash="argon2-secret-hash",
        created_at=now,
        updated_at=now,
        is_platform_admin=is_platform_admin,
    )


def test_register_returns_201_and_user_without_hash() -> None:
    user = _user()
    fake = _FakeRegisterUser(result=user)
    client = next(_client(fake))
    resp = client.post(
        "/api/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": _VALID_PASSWORD,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body == {
        "id": str(user.id.value),
        "username": "alice",
        "email": "alice@example.com",
        "is_platform_admin": False,
    }
    assert "password" not in body
    assert "password_hash" not in body
    assert "argon2-secret-hash" not in resp.text


def test_register_duplicate_username_returns_409() -> None:
    fake = _FakeRegisterUser(error=UsernameAlreadyExistsError("alice"))
    client = next(_client(fake))
    resp = client.post(
        "/api/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": _VALID_PASSWORD,
        },
    )
    assert resp.status_code == 409


def test_register_duplicate_email_returns_409() -> None:
    fake = _FakeRegisterUser(error=EmailAlreadyExistsError("alice@example.com"))
    client = next(_client(fake))
    resp = client.post(
        "/api/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": _VALID_PASSWORD,
        },
    )
    assert resp.status_code == 409


def test_register_disabled_returns_403() -> None:
    fake = _FakeRegisterUser(error=RegistrationDisabledError())
    client = next(_client(fake))
    resp = client.post(
        "/api/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": _VALID_PASSWORD,
        },
    )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "registration_disabled"


def test_register_throttled_returns_429() -> None:
    fake = _FakeRegisterUser(error=RegistrationThrottledError())
    client = next(_client(fake))
    resp = client.post(
        "/api/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": _VALID_PASSWORD,
        },
    )
    assert resp.status_code == 429
    assert resp.json()["reason"] == "registration_throttled"


def test_register_weak_password_returns_422_with_reason_no_echo() -> None:
    fake = _FakeRegisterUser(error=PasswordPolicyError("too_short"))
    client = next(_client(fake))
    weak = "Qz9!secretpw"
    resp = client.post(
        "/api/users",
        json={"username": "alice", "email": "alice@example.com", "password": weak},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "too_short"
    # The submitted password must never appear in the response body.
    assert weak not in resp.text


def test_register_password_over_schema_bound_returns_422() -> None:
    # A >1024-character password is rejected by the schema before the use case
    # runs (cheap DoS guard); the use case is never called.
    fake = _FakeRegisterUser(result=_user())
    client = next(_client(fake))
    resp = client.post(
        "/api/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "x" * 1025,
        },
    )
    assert resp.status_code == 422
    assert fake.calls == []


def test_register_records_only_auth_register_for_non_admin() -> None:
    # An ordinary (non-first) registration records just the auth:register row.
    fake = _FakeRegisterUser(result=_user(is_platform_admin=False))
    recorder = RecordingAuditRecorder()
    client = next(_client(fake, recorder=recorder))
    resp = client.post(
        "/api/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": _VALID_PASSWORD,
        },
    )
    assert resp.status_code == 201
    assert [e.operation for e in recorder.events] == [ops.AUTH_REGISTER]


def test_first_user_bootstrap_records_platform_admin_grant() -> None:
    # The first registrant is auto-granted platform admin (#909): the grant is
    # audited explicitly, attributed to the new user as both actor and target.
    user = _user(is_platform_admin=True)
    fake = _FakeRegisterUser(result=user)
    recorder = RecordingAuditRecorder()
    client = next(_client(fake, recorder=recorder))
    resp = client.post(
        "/api/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": _VALID_PASSWORD,
        },
    )
    assert resp.status_code == 201
    assert [e.operation for e in recorder.events] == [
        ops.AUTH_REGISTER,
        ops.USER_PLATFORM_ADMIN_GRANT,
    ]
    grant = recorder.events[1]
    assert grant.outcome == Outcome.SUCCESS
    assert grant.actor_id == user.id.value
    assert grant.target_id == user.id.value
    assert grant.target_type == ops.TARGET_USER


@pytest.mark.parametrize("missing", ["username", "email", "password"])
def test_register_missing_field_returns_422(missing: str) -> None:
    fake = _FakeRegisterUser(result=_user())
    client = next(_client(fake))
    payload = {
        "username": "alice",
        "email": "alice@example.com",
        "password": _VALID_PASSWORD,
    }
    del payload[missing]
    resp = client.post("/api/users", json=payload)
    assert resp.status_code == 422
