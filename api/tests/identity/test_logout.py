"""Unit tests for the Logout use case (refresh-token revocation, FR-AUTH-3)."""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.identity.application.logout import Logout
from mc_server_dashboard_api.identity.domain.entities import RefreshToken
from mc_server_dashboard_api.identity.domain.value_objects import RefreshTokenId, UserId
from tests.identity.fakes import FakeClock, FakeTokenService, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc)


def _logout(uow: FakeUnitOfWork) -> Logout:
    return Logout(uow=uow, tokens=FakeTokenService(), clock=FakeClock(_NOW))


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
