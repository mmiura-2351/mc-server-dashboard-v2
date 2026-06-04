"""Endpoint tests for POST /users with the RegisterUser use case faked.

The HTTP boundary is exercised in-process via FastAPI's TestClient; the use case
is overridden so no database is touched (NFR-TEST-1). Verifies the success code,
the response shape (never the hash), and the policy/duplicate error mappings.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import get_register_user
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.errors import (
    EmailAlreadyExistsError,
    PasswordPolicyError,
    UsernameAlreadyExistsError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)

_VALID_PASSWORD = "Wm7!qz#Lp2vT"


class _FakeRegisterUser:
    def __init__(self, *, result: User | None = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.calls: list[dict[str, str]] = []

    async def __call__(self, *, username: str, email: str, password: str) -> User:
        self.calls.append({"username": username, "email": email, "password": password})
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _client(use_case: _FakeRegisterUser) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_register_user] = lambda: use_case
    with TestClient(app) as client:
        yield client


def _user() -> User:
    now = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
    return User(
        id=UserId.new(),
        username=Username("alice"),
        email=EmailAddress("alice@example.com"),
        password_hash="argon2-secret-hash",
        created_at=now,
        updated_at=now,
        is_platform_admin=False,
    )


def test_register_returns_201_and_user_without_hash() -> None:
    user = _user()
    fake = _FakeRegisterUser(result=user)
    client = next(_client(fake))
    resp = client.post(
        "/users",
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
        "/users",
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
        "/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": _VALID_PASSWORD,
        },
    )
    assert resp.status_code == 409


def test_register_weak_password_returns_422_with_reason_no_echo() -> None:
    fake = _FakeRegisterUser(error=PasswordPolicyError("too_short"))
    client = next(_client(fake))
    weak = "Qz9!secretpw"
    resp = client.post(
        "/users",
        json={"username": "alice", "email": "alice@example.com", "password": weak},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"]["reason"] == "too_short"
    # The submitted password must never appear in the response body.
    assert weak not in resp.text


def test_register_password_over_schema_bound_returns_422() -> None:
    # A >1024-character password is rejected by the schema before the use case
    # runs (cheap DoS guard); the use case is never called.
    fake = _FakeRegisterUser(result=_user())
    client = next(_client(fake))
    resp = client.post(
        "/users",
        json={
            "username": "alice",
            "email": "alice@example.com",
            "password": "x" * 1025,
        },
    )
    assert resp.status_code == 422
    assert fake.calls == []


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
    resp = client.post("/users", json=payload)
    assert resp.status_code == 422
