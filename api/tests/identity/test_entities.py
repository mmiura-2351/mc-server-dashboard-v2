"""Unit tests for the identity entities (pure, no I/O)."""

import datetime as dt

from mc_server_dashboard_api.identity.domain.entities import RefreshToken, User
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    RefreshTokenId,
    UserId,
    Username,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def _user() -> User:
    return User(
        id=UserId.new(),
        username=Username("alice"),
        email=EmailAddress("alice@example.com"),
        password_hash="hash",
        is_platform_admin=False,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_user_defaults_to_non_admin() -> None:
    # FR-AUTH-6 / DATABASE.md: the admin axis defaults to false.
    user = User(
        id=UserId.new(),
        username=Username("bob"),
        email=EmailAddress("bob@example.com"),
        password_hash="hash",
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert user.is_platform_admin is False


def test_user_carries_the_platform_admin_flag() -> None:
    user = User(
        id=UserId.new(),
        username=Username("root"),
        email=EmailAddress("root@example.com"),
        password_hash="hash",
        is_platform_admin=True,
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert user.is_platform_admin is True


def test_refresh_token_defaults_to_not_revoked() -> None:
    token = RefreshToken(
        id=RefreshTokenId.new(),
        user_id=UserId.new(),
        token_hash="hashed",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=30),
    )
    assert token.revoked_at is None


def test_refresh_token_is_active_when_unexpired_and_unrevoked() -> None:
    token = RefreshToken(
        id=RefreshTokenId.new(),
        user_id=UserId.new(),
        token_hash="hashed",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=30),
    )
    assert token.is_active(now=_NOW) is True


def test_refresh_token_is_inactive_once_expired() -> None:
    token = RefreshToken(
        id=RefreshTokenId.new(),
        user_id=UserId.new(),
        token_hash="hashed",
        issued_at=_NOW,
        expires_at=_NOW,
    )
    assert token.is_active(now=_NOW + dt.timedelta(seconds=1)) is False


def test_refresh_token_is_inactive_once_revoked() -> None:
    token = RefreshToken(
        id=RefreshTokenId.new(),
        user_id=UserId.new(),
        token_hash="hashed",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(days=30),
        revoked_at=_NOW,
    )
    assert token.is_active(now=_NOW) is False
