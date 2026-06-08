"""Unit tests for the Logout use case (refresh-token revocation, FR-AUTH-3)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.domain.entities import (
    REVOKED_LOGOUT,
    REVOKED_SUPERSEDED,
    RefreshToken,
)
from mc_server_dashboard_api.identity.domain.value_objects import RefreshTokenId, UserId
from tests.identity.fakes import FakeClock, FakeTokenService, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _logout(uow: FakeUnitOfWork) -> Logout:
    return Logout(uow=uow, tokens=FakeTokenService(), clock=FakeClock(_NOW))


def _seed(uow: FakeUnitOfWork, *, secret: str) -> str:
    token_hash = f"hash::{secret}"
    uow.refresh_tokens.seed(
        RefreshToken(
            id=RefreshTokenId.new(),
            user_id=UserId.new(),
            token_hash=token_hash,
            issued_at=_NOW,
            expires_at=_NOW + dt.timedelta(days=14),
        )
    )
    return token_hash


async def test_logout_revokes_the_token() -> None:
    uow = FakeUnitOfWork()
    uow.refresh_tokens.seed(
        RefreshToken(
            id=RefreshTokenId.new(),
            user_id=UserId.new(),
            token_hash="hash::session",
            issued_at=_NOW,
            expires_at=_NOW + dt.timedelta(days=14),
        )
    )

    await _logout(uow)(refresh_token="session")

    assert uow.refresh_tokens.by_hash["hash::session"].revoked_at == _NOW
    assert uow.commits == 1


async def test_logout_unknown_token_is_idempotent() -> None:
    uow = FakeUnitOfWork()
    await _logout(uow)(refresh_token="never-issued")
    assert uow.refresh_tokens.by_hash == {}
    assert uow.commits == 1


async def test_logout_revokes_both_body_and_superseded_cookie_token() -> None:
    # Both-transports logout: the body token is revoked as ``logout`` and the
    # superseded cookie token is revoked too, as ``superseded`` (issue #384).
    uow = FakeUnitOfWork()
    body_hash = _seed(uow, secret="body-token")
    cookie_hash = _seed(uow, secret="cookie-token")

    await _logout(uow)(refresh_token="body-token", superseded_token="cookie-token")

    assert uow.refresh_tokens.by_hash[body_hash].revoked_at == _NOW
    assert uow.refresh_tokens.by_hash[body_hash].revoked_reason == REVOKED_LOGOUT
    assert uow.refresh_tokens.by_hash[cookie_hash].revoked_at == _NOW
    assert uow.refresh_tokens.by_hash[cookie_hash].revoked_reason == REVOKED_SUPERSEDED
    assert uow.commits == 1


async def test_logout_same_token_in_both_transports_is_not_double_revoked() -> None:
    # The cookie carried the same token as the body: revoked once as ``logout``,
    # not re-stamped ``superseded``.
    uow = FakeUnitOfWork()
    token_hash = _seed(uow, secret="same-token")

    await _logout(uow)(refresh_token="same-token", superseded_token="same-token")

    assert uow.refresh_tokens.by_hash[token_hash].revoked_reason == REVOKED_LOGOUT


async def test_logout_unknown_superseded_token_is_idempotent() -> None:
    # A malformed / never-issued cookie token alongside a valid body token must not
    # fail logout.
    uow = FakeUnitOfWork()
    body_hash = _seed(uow, secret="body-token")

    await _logout(uow)(refresh_token="body-token", superseded_token="never-issued")

    assert uow.refresh_tokens.by_hash[body_hash].revoked_at == _NOW
    assert "hash::never-issued" not in uow.refresh_tokens.by_hash
