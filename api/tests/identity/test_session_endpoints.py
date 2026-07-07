"""Endpoint tests for the session-management routes on /users/me/sessions (#387).

The use cases are overridden with fakes so no database is touched (NFR-TEST-1).
Verifies the listing shape (no token hash), the 404-not-403 rule for an unknown /
malformed / other-user session id, that the DELETEs return 204 and audit, and the
argument threading for everywhere-else logout.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_current_user,
    get_list_sessions,
    get_revoke_other_sessions,
    get_revoke_session,
)
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    UserId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.identity.fakes import make_user

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


class _Fake:
    def __init__(self, result: object = None) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self._result


def _provider(value: object) -> Callable[[], object]:
    def _provide() -> object:
        return value

    return _provide


_PROVIDERS = {
    "list_sessions": get_list_sessions,
    "revoke_session": get_revoke_session,
    "revoke_other_sessions": get_revoke_other_sessions,
    "recorder": get_audit_recorder,
}


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _client(user: object, **overrides: object) -> Iterator[TestClient]:
    app = _shared_app
    app.dependency_overrides[get_current_user] = _provider(user)
    for dependency, value in overrides.items():
        app.dependency_overrides[_PROVIDERS[dependency]] = _provider(value)
    with TestClient(app) as client:
        yield client


def _session(user_id: UserId) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id,
        token_hash="hash::secret",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=14),
    )


# --- GET /users/me/sessions ------------------------------------------------


def test_list_returns_safe_metadata_without_token_hash() -> None:
    user = make_user()
    token = _session(user.id)
    fake = _Fake(result=[token])
    client = next(_client(user, list_sessions=fake))

    resp = client.get("/api/users/me/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert body == [
        {
            "id": str(token.id.value),
            # Canonical RFC 3339 UTC form: ``Z`` suffix, not ``+00:00`` (issue #632).
            "created_at": "2026-06-04T00:00:00Z",
            "expires_at": "2026-06-18T00:00:00Z",
        }
    ]
    # The token hash/secret is never serialised.
    assert "hash" not in resp.text and "secret" not in resp.text
    assert fake.calls == [{"user_id": user.id}]


# --- DELETE /users/me/sessions/{id} ----------------------------------------


def test_revoke_one_returns_204_and_audits() -> None:
    user = make_user()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=True)
    client = next(_client(user, revoke_session=fake, recorder=recorder))
    session_id = uuid.uuid4()

    resp = client.delete(f"/api/users/me/sessions/{session_id}")

    assert resp.status_code == 204
    assert fake.calls == [
        {"user_id": user.id, "session_id": RefreshTokenId(session_id)}
    ]
    assert [e.operation for e in recorder.events] == [ops.AUTH_SESSION_REVOKE]
    assert recorder.events[0].actor_id == user.id.value
    assert recorder.events[0].outcome == Outcome.SUCCESS


def test_revoke_one_unknown_or_other_user_returns_404_not_403() -> None:
    user = make_user()
    recorder = RecordingAuditRecorder()
    # The use case reports a miss for both an unknown id and another user's id.
    fake = _Fake(result=False)
    client = next(_client(user, revoke_session=fake, recorder=recorder))

    resp = client.delete(f"/api/users/me/sessions/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["reason"] == "session_not_found"
    # A miss is not audited.
    assert recorder.events == []


def test_revoke_one_malformed_id_returns_404() -> None:
    user = make_user()
    fake = _Fake(result=True)
    client = next(_client(user, revoke_session=fake))

    resp = client.delete("/api/users/me/sessions/not-a-uuid")

    assert resp.status_code == 404
    assert resp.json()["reason"] == "session_not_found"
    # The use case is never reached for a malformed id.
    assert fake.calls == []


# --- DELETE /users/me/sessions (everywhere-else logout) --------------------


def test_revoke_others_passes_presented_token_and_audits() -> None:
    user = make_user()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(user, revoke_other_sessions=fake, recorder=recorder))

    resp = client.request(
        "DELETE",
        "/api/users/me/sessions",
        json={"refresh_token": "current"},
    )

    assert resp.status_code == 204
    assert fake.calls == [
        {
            "user_id": user.id,
            "current_refresh_token": "current",
            "keep_session_id": None,
        }
    ]
    assert [e.operation for e in recorder.events] == [ops.AUTH_SESSION_REVOKE]


def test_revoke_others_without_body_passes_none() -> None:
    user = make_user()
    fake = _Fake(result=None)
    client = next(_client(user, revoke_other_sessions=fake))

    resp = client.delete("/api/users/me/sessions")

    assert resp.status_code == 204
    assert fake.calls == [
        {
            "user_id": user.id,
            "current_refresh_token": None,
            "keep_session_id": None,
        }
    ]


def test_revoke_others_with_keep_session_id() -> None:
    """keep_session_id is threaded to the use case as a RefreshTokenId (#606)."""
    user = make_user()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=None)
    client = next(_client(user, revoke_other_sessions=fake, recorder=recorder))
    session_id = uuid.uuid4()

    resp = client.request(
        "DELETE",
        "/api/users/me/sessions",
        json={"keep_session_id": str(session_id)},
    )

    assert resp.status_code == 204
    assert fake.calls == [
        {
            "user_id": user.id,
            "current_refresh_token": None,
            "keep_session_id": RefreshTokenId(session_id),
        }
    ]
    assert [e.operation for e in recorder.events] == [ops.AUTH_SESSION_REVOKE]
