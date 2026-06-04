"""Unit tests for the RefreshSession use case (rotation + reuse policy).

Covers a valid rotation (old revoked, new pair issued in one commit), rejection
of expired/revoked/unknown tokens, and the reuse-after-rotation policy: a second
presentation of an already-revoked token revokes the whole family.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mc_server_dashboard_api.identity.application.refresh_session import RefreshSession
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.errors import InvalidRefreshTokenError
from mc_server_dashboard_api.identity.domain.value_objects import RefreshTokenId, UserId
from tests.identity.fakes import FakeClock, FakeTokenService, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)
_REFRESH_TTL = dt.timedelta(days=14)
_USER = UserId.new()


def _refresh(uow: FakeUnitOfWork, clock: FakeClock) -> RefreshSession:
    return RefreshSession(
        uow=uow,
        tokens=FakeTokenService(),
        clock=clock,
        refresh_ttl=_REFRESH_TTL,
    )


def _seed_token(
    uow: FakeUnitOfWork,
    *,
    secret: str = "old-secret",
    expires_at: dt.datetime | None = None,
    revoked_at: dt.datetime | None = None,
) -> str:
    token_hash = f"hash::{secret}"
    uow.refresh_tokens.seed(
        RefreshToken(
            id=RefreshTokenId.new(),
            user_id=_USER,
            token_hash=token_hash,
            issued_at=_NOW - dt.timedelta(days=1),
            expires_at=expires_at or (_NOW + _REFRESH_TTL),
            revoked_at=revoked_at,
        )
    )
    return token_hash


async def test_rotation_revokes_old_and_issues_new_pair() -> None:
    uow = FakeUnitOfWork()
    old_hash = _seed_token(uow, secret="old-secret")
    clock = FakeClock(_NOW)

    pair = await _refresh(uow, clock)(refresh_token="old-secret")

    assert pair.access_token == f"access::{_USER.value}"
    assert pair.refresh_token == "refresh-secret-1"
    # Old token now revoked; new token persisted and active.
    assert uow.refresh_tokens.by_hash[old_hash].revoked_at == _NOW
    new = uow.refresh_tokens.by_hash["hash::refresh-secret-1"]
    assert new.user_id == _USER
    assert new.revoked_at is None
    assert uow.commits == 1


async def test_unknown_token_is_rejected() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(uow, FakeClock(_NOW))(refresh_token="nope")
    assert uow.commits == 0


async def test_expired_token_is_rejected() -> None:
    uow = FakeUnitOfWork()
    _seed_token(uow, secret="old-secret", expires_at=_NOW - dt.timedelta(seconds=1))
    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(uow, FakeClock(_NOW))(refresh_token="old-secret")
    assert uow.commits == 0


async def test_already_revoked_token_is_rejected() -> None:
    uow = FakeUnitOfWork()
    _seed_token(uow, secret="old-secret", revoked_at=_NOW - dt.timedelta(minutes=1))
    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(uow, FakeClock(_NOW))(refresh_token="old-secret")


async def test_reuse_after_rotation_revokes_the_family() -> None:
    # Two live tokens for the user; the first is already-rotated (revoked).
    uow = FakeUnitOfWork()
    reused_hash = _seed_token(
        uow, secret="old-secret", revoked_at=_NOW - dt.timedelta(minutes=1)
    )
    sibling_hash = _seed_token(uow, secret="sibling-secret")
    clock = FakeClock(_NOW)

    with pytest.raises(InvalidRefreshTokenError):
        await _refresh(uow, clock)(refresh_token="old-secret")

    # The still-active sibling is revoked too (family revoke), and committed.
    assert uow.refresh_tokens.by_hash[sibling_hash].revoked_at == _NOW
    # The already-revoked token keeps its original revocation time.
    assert uow.refresh_tokens.by_hash[reused_hash].revoked_at == _NOW - dt.timedelta(
        minutes=1
    )
    assert uow.commits == 1
