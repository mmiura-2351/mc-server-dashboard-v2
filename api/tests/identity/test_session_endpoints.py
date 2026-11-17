"""Endpoint tests for the session-management routes on /users/me/sessions (#387).

The use cases are overridden with fakes so no database is touched (NFR-TEST-1).
Verifies the listing shape (no token hash), the 404-not-403 rule for an unknown /
malformed / other-user session id, that the DELETEs return 204 and audit, and the
argument threading for everywhere-else logout.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable

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
from mc_server_dashboard_api.identity.adapters.token_service import JwtTokenService
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.value_objects import (
    RefreshTokenId,
    RotationChainId,
    UserId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.client_utils import enter_client
from tests.identity.fakes import FakeClock, make_user

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


def _client(user: object, **overrides: object) -> TestClient:
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = _provider(user)
    for dependency, value in overrides.items():
        app.dependency_overrides[_PROVIDERS[dependency]] = _provider(value)
    return enter_client(TestClient(app))


def _session(user_id: UserId) -> RefreshToken:
    return RefreshToken(
        id=RefreshTokenId.new(),
        user_id=user_id,
        chain_id=RotationChainId.new(),
        token_hash="hash::secret",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=14),
    )


# --- GET /users/me/sessions ------------------------------------------------


def test_list_returns_safe_metadata_without_token_hash() -> None:
    user = make_user()
    token = _session(user.id)
    fake = _Fake(result=[token])
    client = _client(user, list_sessions=fake)

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


def test_list_declares_no_store() -> None:
    # The listing is per-user data — session ids and timestamps (issue #2587).
    user = make_user()
    fake = _Fake(result=[_session(user.id)])
    client = _client(user, list_sessions=fake)

    resp = client.get("/api/users/me/sessions")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"


# --- DELETE /users/me/sessions/{id} ----------------------------------------


def test_revoke_one_returns_204_and_audits() -> None:
    user = make_user()
    recorder = RecordingAuditRecorder()
    fake = _Fake(result=True)
    client = _client(user, revoke_session=fake, recorder=recorder)
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
    client = _client(user, revoke_session=fake, recorder=recorder)

    resp = client.delete(f"/api/users/me/sessions/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["reason"] == "session_not_found"
    # A miss is not audited.
    assert recorder.events == []


def test_revoke_one_malformed_id_returns_404() -> None:
    user = make_user()
    fake = _Fake(result=True)
    client = _client(user, revoke_session=fake)

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
    client = _client(user, revoke_other_sessions=fake, recorder=recorder)

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


def test_revoke_others_declares_no_store() -> None:
    # A 204 has no body to store; declared so both methods on the exact path
    # /users/me/sessions state the same policy (issue #2587). The per-id sibling
    # is a different path, and records at the route why it declares none
    # (issue #2763).
    user = make_user()
    fake = _Fake(result=None)
    client = _client(user, revoke_other_sessions=fake)

    resp = client.delete("/api/users/me/sessions")

    assert resp.status_code == 204
    assert resp.headers["cache-control"] == "no-store"


def test_revoke_others_without_body_passes_none() -> None:
    user = make_user()
    fake = _Fake(result=None)
    client = _client(user, revoke_other_sessions=fake)

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
    client = _client(user, revoke_other_sessions=fake, recorder=recorder)
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


# --- Request-size caps on DELETE /users/me/sessions ---------------------------
#
# A DoS bound of 256 characters on each field; a real refresh token is 43 and a
# session id is a 36-character UUID.


def _real_refresh_token() -> str:
    """A refresh secret exactly as the adapter issues it."""

    hour = dt.timedelta(hours=1)
    service = JwtTokenService(
        signing_key="0123456789abcdef0123456789abcdef",
        algorithm="HS256",
        access_ttl=hour,
        download_grant_ttl=hour,
        download_cookie_ttl=hour,
        clock=FakeClock(dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)),
    )
    return service.issue_refresh_token().secret


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("refresh_token", _real_refresh_token()),
        ("refresh_token", "A" * 256),
        ("keep_session_id", str(uuid.uuid4())),
        ("keep_session_id", "A" * 256),
    ],
    ids=["real-token", "token-at-cap", "real-session-id", "session-id-at-cap"],
)
def test_revoke_others_accepts_a_field_within_the_cap(field: str, value: str) -> None:
    user = make_user()
    fake = _Fake(result=None)
    client = _client(user, revoke_other_sessions=fake)

    resp = client.request("DELETE", "/api/users/me/sessions", json={field: value})

    assert resp.status_code == 204
    assert len(fake.calls) == 1


@pytest.mark.parametrize("field", ["refresh_token", "keep_session_id"])
def test_revoke_others_rejects_a_field_past_the_cap(field: str) -> None:
    user = make_user()
    fake = _Fake(result=None)
    client = _client(user, revoke_other_sessions=fake)

    resp = client.request("DELETE", "/api/users/me/sessions", json={field: "A" * 257})

    assert resp.status_code == 422
    assert resp.json()["reason"] == "validation_error"
    assert fake.calls == []
