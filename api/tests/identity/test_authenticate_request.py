"""Unit tests for the AuthenticateRequest use case (current-user resolution)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.identity.application.authenticate_request import (
    AuthenticateRequest,
)
from mc_server_dashboard_api.identity.domain.errors import InvalidAccessTokenError
from tests.identity.fakes import FakeTokenService, FakeUnitOfWork, make_user


def _auth(uow: FakeUnitOfWork) -> AuthenticateRequest:
    return AuthenticateRequest(uow=uow, tokens=FakeTokenService())


async def test_valid_token_returns_user() -> None:
    user = make_user()
    uow = FakeUnitOfWork()
    uow.users.seed(user)

    resolved = await _auth(uow)(access_token=f"access::{user.id.value}")

    assert resolved.id == user.id


async def test_invalid_token_is_rejected() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidAccessTokenError):
        await _auth(uow)(access_token="garbage")


async def test_valid_token_for_missing_user_is_rejected() -> None:
    user = make_user()  # not seeded
    uow = FakeUnitOfWork()
    with pytest.raises(InvalidAccessTokenError):
        await _auth(uow)(access_token=f"access::{user.id.value}")
