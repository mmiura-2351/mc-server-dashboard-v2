"""Unit tests for the RestoreSession use case (non-rotating session restore).

The bootstrap path validates a refresh cookie into an access token *without*
rotating the refresh token (issue #512). An active token yields an access token
and leaves the family untouched (no DB write, no revoke); an unknown / expired /
revoked token is rejected with :class:`InvalidRefreshTokenError`. Crucially,
re-presenting a *rotated* predecessor does NOT trigger reuse detection here — a
restore never rotates, so it can never create a torn-rotation race, and so it
must never revoke the family the way :class:`RefreshSession` does.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.restore_session import RestoreSession
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_FAMILY,
    REVOKED_ROTATED,
    RefreshToken,
)
from mc_server_dashboard_api.identity.domain.errors import InvalidRefreshTokenError
from mc_server_dashboard_api.identity.domain.value_objects import RefreshTokenId, UserId
from tests.identity.fakes import FakeClock, FakeTokenService, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_REFRESH_TTL = dt.timedelta(days=14)
_USER = UserId.new()


def _restore(uow: FakeUnitOfWork, clock: FakeClock) -> RestoreSession:
    return RestoreSession(uow=uow, tokens=FakeTokenService(), clock=clock)


def _seed_token(
    uow: FakeUnitOfWork,
    *,
    secret: str = "live-secret",
    expires_at: dt.datetime | None = None,
    revoked_at: dt.datetime | None = None,
    revoked_reason: str | None = None,
) -> str:
    if revoked_at is not None and revoked_reason is None:
        revoked_reason = REVOKED_ROTATED
    token_hash = f"hash::{secret}"
    uow.refresh_tokens.seed(
        RefreshToken(
            id=RefreshTokenId.new(),
            user_id=_USER,
            token_hash=token_hash,
            issued_at=_NOW - dt.timedelta(days=1),
            expires_at=expires_at or (_NOW + _REFRESH_TTL),
            revoked_at=revoked_at,
            revoked_reason=revoked_reason,
        )
    )
    return token_hash


async def test_active_token_yields_access_token_without_rotation() -> None:
    uow = FakeUnitOfWork()
    seeded_hash = _seed_token(uow, secret="live-secret")
    clock = FakeClock(_NOW)

    result = await _restore(uow, clock)(refresh_token="live-secret")

    assert result.access_token == f"access::{_USER.value}"
    # The session's user id rides the result so the route can attribute the
    # auth:session_restore SUCCESS audit row (issue #530).
    assert result.user_id == _USER.value
    # No new refresh row was minted (the only row is the one we seeded).
    assert set(uow.refresh_tokens.by_hash) == {seeded_hash}
    # The presented token is left active — not rotated, not revoked.
    assert uow.refresh_tokens.by_hash[seeded_hash].revoked_at is None
    # No transaction writes were committed (pure read).
    assert uow.commits == 0


async def test_unknown_token_is_rejected() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidRefreshTokenError):
        await _restore(uow, FakeClock(_NOW))(refresh_token="nope")
    assert uow.commits == 0


async def test_expired_token_is_rejected() -> None:
    uow = FakeUnitOfWork()
    _seed_token(uow, secret="live-secret", expires_at=_NOW - dt.timedelta(seconds=1))
    with pytest.raises(InvalidRefreshTokenError):
        await _restore(uow, FakeClock(_NOW))(refresh_token="live-secret")
    assert uow.commits == 0


async def test_rotated_predecessor_is_rejected_without_family_revoke() -> None:
    # A rotated (already-revoked) predecessor re-presented to the restore path is
    # simply rejected. Unlike RefreshSession it does NOT trigger reuse detection:
    # restore never rotates, so repeated restore calls cannot be a torn rotation,
    # and must not nuke the family (issue #512). The sibling stays usable.
    uow = FakeUnitOfWork()
    reused_hash = _seed_token(
        uow, secret="old-secret", revoked_at=_NOW - dt.timedelta(seconds=5)
    )
    sibling_hash = _seed_token(uow, secret="sibling-secret")
    clock = FakeClock(_NOW)

    with pytest.raises(InvalidRefreshTokenError):
        await _restore(uow, clock)(refresh_token="old-secret")

    # The sibling session is untouched: no family revocation occurred.
    assert uow.refresh_tokens.by_hash[sibling_hash].revoked_at is None
    # The rejected token keeps its original revocation time (no re-revoke).
    assert uow.refresh_tokens.by_hash[reused_hash].revoked_at == _NOW - dt.timedelta(
        seconds=5
    )
    assert uow.commits == 0


async def test_family_revoked_token_is_rejected_without_re_revoke() -> None:
    # A family-revoked token (theft response) re-presented to restore is rejected
    # with the plain invalid-token error and no further family action — restore
    # has no reuse-detection responsibility.
    uow = FakeUnitOfWork()
    revoked_hash = _seed_token(
        uow,
        secret="stolen",
        revoked_at=_NOW - dt.timedelta(seconds=5),
        revoked_reason=REVOKED_FAMILY,
    )
    clock = FakeClock(_NOW)

    with pytest.raises(InvalidRefreshTokenError):
        await _restore(uow, clock)(refresh_token="stolen")

    assert uow.refresh_tokens.by_hash[revoked_hash].revoked_at == _NOW - dt.timedelta(
        seconds=5
    )
    assert uow.commits == 0


async def test_repeated_restore_is_idempotent() -> None:
    # The bootstrap race class that motivated this endpoint: many rapid page
    # reloads each restore against the same live cookie. Every call yields a token
    # and never mutates the family.
    uow = FakeUnitOfWork()
    seeded_hash = _seed_token(uow, secret="live-secret")
    clock = FakeClock(_NOW)
    restore = _restore(uow, clock)

    first = await restore(refresh_token="live-secret")
    second = await restore(refresh_token="live-secret")

    assert first == second
    assert first.access_token == f"access::{_USER.value}"
    assert first.user_id == _USER.value
    assert set(uow.refresh_tokens.by_hash) == {seeded_hash}
    assert uow.refresh_tokens.by_hash[seeded_hash].revoked_at is None
    assert uow.commits == 0
